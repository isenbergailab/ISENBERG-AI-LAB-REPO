"""Whole files dropped into the Inbox folder, Markdown or PDF: read, extract, propose.

A syllabus becomes dated tasks; a transcript becomes a summary plus follow-up actions. The file itself moves
to 04_REFERENCE once you approve. Every item is a separate checkbox in APPROVAL, and Python checks every date.
Private files never reach a model: they can only be filed as they are. A ExampleCorp mention in Markdown is ordinary text
(restricted work never enters the vault; recruiting notes are not restricted work, see docs/DOMAIN.md).

A PDF is read locally with pypdf. Its text passes the sensitive screen first, as mail does: a hit sends nothing and
asks you to file it as is or let the model read it. On approval the PDF moves beside a reference note that embeds
it.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .clarification import area_candidates, compile_ops, new_marker, project_candidates, project_note_text
from .core import CONTEXTS, SECRET_RE, digest
from .pdf import INSTALL, PdfError, pdf_text, version as pdf_version
from .dates import resolve_date_evidence
from .markdown import iter_links, link_target, parse_frontmatter, split_frontmatter
from .providers import ProviderError
from .screen import screen
from .tasks import TAG_RE, clean_text, format_task
from .vault import Vault, project_key

if TYPE_CHECKING:  # pragma: no cover
    from .housekeeping import Housekeeper

MIN_AGE_SECONDS = 120          # leave a file alone while you are still writing or pasting it
RETRY_MINUTES = 30             # after a failed model call
MAX_FILES_PER_SCAN = 20
DOC_TYPES = ("syllabus", "transcript", "notes", "other")
KINDS = ("assignment", "exam", "other")
SKIP_FOLDERS = {"SYSTEM", "RESEARCH"}
FILE_TYPES = {".md", ".pdf"}
READ_ANYWAY = "Read it with the model"
OWN_KEYS = {"type", "project", "area", "doc_type", "source", "filed", "new_project", "start", "outcome"}
WEEKDAY_RE = re.compile(r"(?i)\b(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|rsday|urday)?\b\.?,?")
DATE_BITS = re.compile(
    r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b")


def _fmt(day: date) -> str:
    return f"{day:%a %b} {day.day}"


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _plain(value: Any, maximum: int) -> str:
    """One line of harmless text: no comments, links or tags; trimmed to maximum."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("<!--", "").replace("-->", "").replace("[[", "").replace("]]", "")
    text = re.sub(r"#(?=\d)", "No. ", text)  # 'Homework #3' is a number, not a tag
    text = TAG_RE.sub("", text).strip()
    return text[:maximum].strip()


def verify_date(evidence: str, value: date, reference: date) -> bool:
    """True when Python reads the same month and day from the quoted evidence (the year may be inferred)."""
    candidates = [evidence, WEEKDAY_RE.sub(" ", evidence).strip(" ,"), *DATE_BITS.findall(evidence)]
    for candidate in candidates:
        candidate = candidate.strip(" ,.")
        if not candidate:
            continue
        try:
            found = resolve_date_evidence(candidate, reference)
        except ValueError:
            continue
        if found is None:
            continue
        if found == value:
            return True
        has_year = bool(re.search(r"\b\d{4}\b|/\d{2}\b", candidate))
        if not has_year and (found.month, found.day) == (value.month, value.day):
            return True
    return False


def _file_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "_", Path(name).stem).strip("_").upper()[:60]
    return stem or "DOCUMENT"


class DocumentIntake:
    def __init__(self, housekeeper: "Housekeeper"):
        self.hk = housekeeper
        self.agent = housekeeper.agent
        self.settings = housekeeper.settings
        self.ledger = housekeeper.ledger

    # ------------------------------------------------------------ discovery
    def files(self) -> list[Path]:
        s = self.settings
        if not s.inbox_dir:
            return []
        folder = s.vault_path(s.inbox_dir)
        if not folder.is_dir():
            return []
        inbox = s.vault_path(s.inbox)
        found = [p for p in sorted(folder.iterdir()) if p.suffix.lower() in FILE_TYPES and p.is_file() and p != inbox
                 and not p.name.startswith(".")]
        return found[:MAX_FILES_PER_SCAN]

    def reference_folders(self) -> list[str]:
        s = self.settings
        root = s.vault_path(s.reference_dir)
        if not root.is_dir():
            return []
        return sorted(f"{s.reference_dir}/{p.name}" for p in root.iterdir()
                      if p.is_dir() and not p.name.startswith(".") and p.name not in SKIP_FOLDERS)

    # ------------------------------------------------------------ privacy
    @staticmethod
    def private_reason(vault: Vault, text: str, front: dict[str, Any]) -> str:
        if str(front.get("private", "")).casefold() == "true" or front.get("private") is True:
            return "it is marked private: true"
        if "#private" in {tag.casefold() for tag in TAG_RE.findall(text)}:
            return "it contains #private"
        for link in iter_links(text):
            target = link.target
            if target in vault.projects and vault.is_private_project(vault.projects[target]):
                return f"it links the private project {target}"
            if target in vault.areas and vault.areas[target].private:
                return f"it links the private area {target}"
        return ""

    def cleared(self, rel: str, sha: str) -> bool:
        """You answered 'Read it with the model' for this PDF after the screen stopped it."""
        import json
        row = self.ledger.get_by_key(f"document|{rel}|{sha[:16]}|sensitive")
        return (row is not None and row["status"] == "committed"
                and json.loads(row["proposal_json"]).get("summary") == READ_ANYWAY)

    def hint(self, vault: Vault, front: dict[str, Any]) -> str:
        for field in ("project", "area"):
            key = link_target(front.get(field))
            if key and (key in vault.projects or key in vault.areas):
                return key
        return ""

    # ------------------------------------------------------------ main loop
    def run(self, vault: Vault, now: datetime | None = None) -> list[str]:
        s = self.settings
        if not s.documents_enabled or s.mode != "approval":
            return []
        now = now or datetime.now()
        messages: list[str] = []
        valid: set[str] = set()
        for path in self.files():
            try:
                if now.timestamp() - path.stat().st_mtime < MIN_AGE_SECONDS:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            sha = digest(raw)
            rel = s.rel(path)
            pdf = path.suffix.lower() == ".pdf"
            problem = ""
            if pdf:
                front: dict[str, Any] = {}
                try:
                    text = pdf_text(raw, s.document_max_chars + 1)
                except PdfError as exc:
                    text, problem = "", str(exc)
            else:
                text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
                if not text.strip():
                    continue  # an empty new note
                front = parse_frontmatter(text)
            if problem:
                state = "nopdf" if pdf_version() is None else "unreadable"
            elif SECRET_RE.search(text):
                state = "secret"
            elif pdf and screen([text, path.name], [], s) and not self.cleared(rel, sha):
                state = "sensitive"
            elif len(text) > s.document_max_chars:
                state = "long"
            elif not pdf and self.private_reason(vault, text, front):
                state = "private"
            elif not s.remote_inference or not self.agent._provider_ready():
                state = "off"
            elif not self.agent.budget.allows("documents"):
                state = "budget"
            else:
                state = "ok"
            key = f"document|{rel}|{sha[:16]}|{state}"
            valid.add(key)
            existing = self.ledger.get_by_key(key)
            if existing is not None:
                if existing["status"] == "stale":
                    self.rebuild(vault, existing, rel, path.name, text, sha, front)
                continue
            failed = self.ledger.kv_get(f"docfail|{sha[:16]}")
            if state == "ok" and failed and datetime.fromisoformat(failed) > now - timedelta(minutes=RETRY_MINUTES):
                continue
            try:
                proposal = self.build(vault, rel, path.name, text, sha, front, state, problem)
            except Exception as exc:  # noqa: BLE001 - one unreadable file must not block the others
                if not isinstance(exc, (ProviderError, ValueError, RuntimeError)):
                    self.agent.health.trace(f"Inbox file {path.name}", exc)
                    exc = f"{type(exc).__name__}: {exc}"
                self.ledger.kv_set(f"docfail|{sha[:16]}", now.isoformat())
                messages.append(f"CHECK: could not read {path.name} ({exc}). Retrying in {RETRY_MINUTES} minutes.")
                continue
            self.hk._propose("document", key, proposal)
        self.hk._expire("document", valid)
        return messages

    # ------------------------------------------------------------ proposals
    def build(self, vault: Vault, rel: str, name: str, text: str, sha: str, front: dict[str, Any],
              state: str, problem: str = "") -> dict[str, Any]:
        s = self.settings
        base = {"kind": "manual", "title": name, "source_file": rel, "ops": []}
        if state == "nopdf":
            return dict(base, reason=f"PDFs are read with pypdf, which is not installed. Run {INSTALL}; the file "
                                     "is read once it is.")
        if state in {"sensitive", "unreadable"}:
            return self.file_as_is(rel, name, sha, state, text, problem)
        if state == "secret":
            return dict(base, reason="It looks like it holds a password or key, so nothing was sent anywhere. "
                                     "Remove that line, or file the note by hand.")
        if state == "long":
            return dict(base, reason=f"It has {len(text):,} characters; the limit is {s.document_max_chars:,}. "
                                     "Split it into smaller files.")
        if state == "off":
            return dict(base, reason="Remote inference is off or the OpenRouter key is missing, so the file was "
                                     "not read. It is picked up once the model is available.")
        if state == "budget":
            return dict(base, reason=f"{self.agent.budget.paused_message()}. This file waits for the monthly model "
                                     "budget and is read once it allows.")
        today = self.agent.today()
        hint = self.hint(vault, front)
        if state == "private":
            reason = self.private_reason(vault, text, front)
            target = hint
            ref_rel = self.reference_path(f"{s.reference_dir}/DOCUMENTS", name)
            note = self.reference_text(text, name, title=Path(name).stem, target=target, doc_type="private",
                                       today=today, summary="", facts=[])
            ops = [{"op": "create_file", "path": ref_rel, "text": note},
                   {"op": "remove_file", "path": rel, "sha": sha}]
            return {"kind": "document", "title": name, "source_file": rel, "ops": ops,
                    "summary": f"Move to {ref_rel} as it is",
                    "reason": f"Private: {reason}. Nothing was sent to a model. Approve to file it as reference "
                              "without extracting dates; add tasks by hand."}
        try:
            self.front_start(front, today)
            self.front_new_project(vault, front)
        except ValueError as exc:
            return dict(base, reason=f"{exc}. Fix the frontmatter; the file is read again once it changes.")
        projects = [p for p in project_candidates(vault, text[:6000]) if p["status"] not in {"done", "dropped"}]
        areas = [a for a in area_candidates(vault) if not vault.areas[a["key"]].private]
        folders = self.reference_folders()
        value = self.agent.provider.document(text, name, today, projects, areas, folders, hint)
        clean = self.validate(value, text, today, {p["key"] for p in projects}, {a["key"] for a in areas},
                              folders, hint)
        proposal = self.compile(vault, rel, name, text, sha, clean, today, front)
        proposal["extraction"] = value
        return proposal

    def rebuild(self, vault: Vault, row: Any, rel: str, name: str, text: str, sha: str,
                front: dict[str, Any]) -> None:
        """A proposal went stale at approval (a note changed). Rebuild it from the saved answer: no new paid call."""
        import json
        from .workflow import PROPOSAL_VERSION
        old = json.loads(row["proposal_json"])
        try:
            if old.get("extraction") is not None:
                today = self.agent.today()
                projects = {p["key"] for p in project_candidates(vault, text[:6000])
                            if p["status"] not in {"done", "dropped"}}
                areas = {a["key"] for a in area_candidates(vault) if not vault.areas[a["key"]].private}
                clean = self.validate(old["extraction"], text, today, projects, areas, self.reference_folders(),
                                      self.hint(vault, front))
                proposal = self.compile(vault, rel, name, text, sha, clean, today, front)
                proposal["extraction"] = old["extraction"]
            else:
                proposal = self.build(vault, rel, name, text, sha, front,
                                      old.get("state") or ("private" if old.get("ops") else "off"))
        except (ProviderError, ValueError, RuntimeError):
            return
        proposal["engine_version"] = PROPOSAL_VERSION
        self.ledger.upsert("document", row["dedupe_key"], proposal)

    def validate(self, value: dict[str, Any], text: str, today: date, projects: set[str], areas: set[str],
                 folders: list[str], hint: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("the model answer is not an object")
        doc_type = value.get("doc_type") if value.get("doc_type") in DOC_TYPES else "other"
        target = str(value.get("target_key") or "").strip()
        if hint:
            target = hint
        elif target and target not in projects and target not in areas:
            target = ""
        folder = str(value.get("reference_folder") or "").strip()
        folder = folder if folder in folders else ""
        squashed = _squash(text)
        notes: list[str] = []
        deadlines, seen = [], set()
        past = unchecked = dropped = 0
        for item in (value.get("deadlines") or [])[:120]:
            if not isinstance(item, dict):
                continue
            try:
                title = _plain(item.get("title"), 150)
                due = date.fromisoformat(str(item.get("date", "")).strip())
                clean_text(title)
            except ValueError:
                dropped += 1
                continue
            evidence = _plain(item.get("date_evidence"), 120)
            if not title or not evidence or _squash(evidence) not in squashed:
                dropped += 1
                continue
            if due < today:
                past += 1
                continue
            if due > today + timedelta(days=730):
                dropped += 1
                continue
            identity = (title.casefold(), due)
            if identity in seen:
                continue
            seen.add(identity)
            verified = verify_date(evidence, due, today)
            unchecked += 0 if verified else 1
            deadlines.append({"title": title, "due": due, "evidence": evidence, "verified": verified,
                              "kind": item.get("kind") if item.get("kind") in KINDS else "other",
                              "context": item.get("context") if item.get("context") in CONTEXTS else "#computer"})
        deadlines.sort(key=lambda d: d["due"])
        actions = []
        for item in (value.get("actions") or [])[:30]:
            if not isinstance(item, dict):
                continue
            title = _plain(item.get("title"), 150)
            evidence = _plain(item.get("evidence"), 200)
            if not title or not evidence or _squash(evidence) not in squashed:
                dropped += 1
                continue
            try:
                clean_text(title)
                due = date.fromisoformat(item["due"]) if item.get("due") else None
            except (ValueError, TypeError):
                dropped += 1
                continue
            if due is not None and (due < today or due > today + timedelta(days=730)):
                due = None
            actions.append({"title": title, "due": due,
                            "context": item.get("context") if item.get("context") in CONTEXTS else "#computer"})
        if past:
            notes.append(f"Skipped {past} date{'s' if past != 1 else ''} already past.")
        if dropped:
            notes.append(f"Dropped {dropped} item{'s' if dropped != 1 else ''} the document did not support.")
        if unchecked:
            notes.append(f"Python could not confirm {unchecked} date{'s' if unchecked != 1 else ''} from the "
                         "quoted text; marked 'check the date'.")
        facts = [f for f in (_plain(f, 300) for f in (value.get("facts") or [])[:15] if isinstance(f, str)) if f]
        summary = re.sub(r"\s+", " ", str(value.get("summary") or "")).replace("<!--", "").replace("-->", "")
        return {"doc_type": doc_type, "title": _plain(value.get("title"), 120), "course": _plain(value.get("course"), 40),
                "target": target, "folder": folder, "summary": summary[:2500].strip(), "facts": facts,
                "deadlines": deadlines, "actions": actions, "notes": notes,
                "explanation": _plain(value.get("explanation"), 300)}

    def file_as_is(self, rel: str, name: str, sha: str, state: str, text: str, problem: str) -> dict[str, Any]:
        """A PDF the model won't read: the screen stopped it, or it has no readable text. File it as is (moved
        beside a bare note, private when the screen stopped it), or, for a screened one, let the model read it."""
        s = self.settings
        sensitive = state == "sensitive"
        ref_rel = self.reference_path(f"{s.reference_dir}/DOCUMENTS", name, pdf=True)
        pdf_rel = ref_rel[:-3] + ".pdf"
        body = ("---\nprivate: true\n---\n" if sensitive else "") + f"![[{pdf_rel.rsplit('/', 1)[-1]}]]"
        note = self.reference_text(body, name, title=Path(name).stem, target="", today=self.agent.today(),
                                   doc_type="private" if sensitive else "other", summary="", facts=[])
        choices = [{"label": "File as is", "ops": [{"op": "create_file", "path": ref_rel, "text": note},
                                                   {"op": "move_file", "path": rel, "to": pdf_rel, "sha": sha}]}]
        if sensitive:
            category = screen([text, name], [], s) or "private"
            reason = (f"The sensitive screen flagged it ({category}), so nothing was sent to a model. File it as is "
                      f"with a private note, or, if it is not private, choose '{READ_ANYWAY}' and it is read at the "
                      "next scan.")
            choices.append({"label": READ_ANYWAY, "ops": [], "consent": True,
                            "note": "No change now; the model reads the PDF at the next scan."})
        else:
            reason = f"The PDF was not read: {problem}. Nothing was sent to a model; file it as is and read it yourself."
        return {"kind": "document", "title": name, "source_file": rel, "ops": [], "state": state,
                "summary": f"{s.reference_dir}/DOCUMENTS/", "reason": reason, "choices": choices}

    def reference_path(self, folder: str, name: str, pdf: bool = False) -> str:
        """A free path for the reference note (and, for a PDF, a free path beside it for the PDF itself)."""
        stem = _file_stem(name)
        rel = f"{folder}/{stem}.md"
        number = 2
        while self.settings.vault_path(rel).exists() or (pdf and self.settings.vault_path(rel[:-3] + ".pdf").exists()):
            rel = f"{folder}/{stem}_{number}.md"
            number += 1
        return rel

    @staticmethod
    def reference_text(text: str, name: str, *, title: str, target: str, doc_type: str, today: date,
                       summary: str, facts: list[str], model: str = "") -> str:
        raw, body_start = split_frontmatter(text)
        kept: list[str] = []
        keep = True
        for line in (raw.split("\n") if raw is not None else []):
            top = re.match(r"^([A-Za-z0-9_.-]+)\s*:", line)
            if top:
                keep = top.group(1) not in OWN_KEYS
            if keep and line.strip():
                kept.append(line)
        body = "\n".join(text.split("\n")[body_start:]).strip("\n") if raw is not None else text.strip("\n")
        body = re.sub(r"<!--\s*gtd-agent.*?-->", "", body, flags=re.S)
        safe_name = name.replace('"', "'")
        lines = ["---", "type: reference", f"project: [\"[[{target}]]\"]" if target else "project: []",
                 f"doc_type: {doc_type}", f"source: \"{safe_name}\"", f"filed: {today.isoformat()}", *kept, "---"]
        body_lines = body.lstrip("\n").split("\n")
        if body_lines and body_lines[0].startswith("# "):
            lines += [body_lines[0], ""]  # keep the document's own title above the summary
            body = "\n".join(body_lines[1:]).strip("\n")
        else:
            lines += [f"# {title}", ""]
        if summary or facts:
            lines += [f"> [!summary] Summary by the GTD agent ({model or 'model'}), {today.isoformat()}"]
            if summary:
                lines.append(f"> {summary}")
            if facts:
                lines += [">", "> **Key facts**", *[f"> - {fact}" for fact in facts]]
            lines += [""]
        lines += [body, ""]
        return "\n".join(lines)

    @staticmethod
    def front_start(front: dict[str, Any], today: date) -> date | None:
        """Frontmatter 'start: YYYY-MM-DD' holds every action from this file until that day."""
        value = front.get("start")
        if value in (None, ""):
            return None
        try:
            day = value if isinstance(value, date) else date.fromisoformat(str(value).strip())
        except ValueError as exc:
            raise ValueError("start: in the file's frontmatter must look like 2026-10-14") from exc
        return day if day > today else None

    @staticmethod
    def front_new_project(vault: Vault, front: dict[str, Any]) -> str:
        """Frontmatter 'new_project: NAME' creates that project; its actions become the project's steps."""
        raw = str(front.get("new_project") or "").strip().strip("[]\"")
        if not raw:
            return ""
        key = project_key(raw)
        if key in vault.projects or key.casefold() in vault.by_stem:
            raise ValueError(f"A note named {key} already exists. Use project: \"[[{key}]]\" instead")
        return key

    def compile(self, vault: Vault, rel: str, name: str, text: str, sha: str, clean: dict[str, Any],
                today: date, front: dict[str, Any] | None = None) -> dict[str, Any]:
        s = self.settings
        front = front or {}
        start_all = self.front_start(front, today)
        new_key = self.front_new_project(vault, front)
        if new_key:
            return self.compile_new_project(vault, rel, name, text, sha, clean, today, front, new_key, start_all)
        target = clean["target"]
        if not target and clean["doc_type"] == "syllabus" and "AREA_SCHOOL" in vault.areas \
                and not vault.areas["AREA_SCHOOL"].private:
            target = "AREA_SCHOOL"
        project = vault.projects.get(target)
        area = vault.areas.get(target) if project is None else None
        if project is not None and project.status in {"done", "dropped"}:
            project, target = None, ""
        folder = (f"{s.reference_dir}/COURSES" if clean["doc_type"] == "syllabus"
                  else clean["folder"] or f"{s.reference_dir}/DOCUMENTS")
        generic = Path(name).stem.casefold().startswith(("untitled", "pasted", "new note"))
        pdf = rel.lower().endswith(".pdf")
        ref_rel = self.reference_path(folder, f"{clean['title']}.md" if generic and clean["title"] else name, pdf=pdf)
        ref_stem = ref_rel.rsplit("/", 1)[-1][:-3]
        title = clean["title"] or Path(name).stem
        note = self.reference_text(f"![[{ref_stem}.pdf]]" if pdf else text, name, title=title, target=target,
                                   doc_type=clean["doc_type"], today=today, summary=clean["summary"],
                                   facts=clean["facts"], model=s.document_model.rsplit("/", 1)[-1])
        base_ops: list[dict[str, Any]] = [{"op": "create_file", "path": ref_rel, "text": note}]
        if project is not None:
            base_ops.append({"op": "append_log", "path": project.rel,
                             "entry": f"{today.isoformat()} Filed [[{ref_stem}]] from the Inbox."})
        base_ops.append({"op": "move_file", "path": rel, "to": f"{ref_rel[:-3]}.pdf", "sha": sha} if pdf
                        else {"op": "remove_file", "path": rel, "sha": sha})

        parked = project is not None and project.status == "someday"
        home = project.rel if project is not None else area.rel if area is not None else s.single_actions
        where = project.key if project is not None else area.key if area is not None else "SINGLE_ACTIONS"
        prefix = clean["course"] if (project is None and clean["course"]) else ""
        items: list[dict[str, Any]] = []
        for deadline in clean["deadlines"]:
            label_title = deadline["title"]
            if prefix and prefix.casefold() not in label_title.casefold():
                label_title = f"{prefix}: {label_title}"
            if deadline["kind"] == "exam":
                task_title = f"Study for {label_title}"
                lead = s.exam_lead_days
            else:
                task_title = label_title
                lead = s.deadline_lead_days
            start = deadline["due"] - timedelta(days=lead) if lead else None
            if start is not None and start <= today:
                start = None
            tags = [deadline["context"]] + ([] if parked else ["#next"])
            marker = f"gtd-{new_marker()}"
            try:
                line = format_task(task_title, tags, dates={"start": start, "due": deadline["due"], "created": today},
                                   block_id=marker)
            except ValueError:
                continue
            label = f"Due {_fmt(deadline['due'])} · {task_title}"
            if start:
                label += f" · shows from {_fmt(start)}"
            if not deadline["verified"]:
                label += f" · check the date (document says '{deadline['evidence']}')"
            label += f" → {where} › Deadlines"
            items.append({"label": label, "ops": [{"op": "add_task", "path": home, "section": "Deadlines",
                                                   "position": "end", "line": line, "marker": f"^{marker}"}]})
        gave_next = False
        for action in clean["actions"]:
            draft = {"route": "next_action", "title": action["title"], "context": action["context"],
                     "project": project, "area": area.key if area is not None else None,
                     "dates": {"due": action["due"], "start": start_all}, "extra_tags": [], "force_queue": gave_next}
            try:
                ops, summary, action_title = compile_ops(vault, draft, capture=action["title"], reference=today,
                                                         marker=new_marker())
            except (ValueError, RuntimeError):
                continue
            if project is not None and any(op.get("position") == "steps" for op in ops):
                gave_next = True
            label = f"Action · {action_title}" + (f" · due {_fmt(action['due'])}" if action["due"] else "")
            label += f" · shows from {_fmt(start_all)}" if start_all else ""
            items.append({"label": f"{label} → {summary}", "ops": ops})
        counts = f"{len(clean['deadlines'])} deadline{'s' if len(clean['deadlines']) != 1 else ''}, " \
                 f"{len(clean['actions'])} action{'s' if len(clean['actions']) != 1 else ''}"
        reason = " ".join([f"{clean['doc_type'].capitalize()} · {counts}.", clean["explanation"], *clean["notes"],
                           f"{s.document_model} read the whole file; Python checked every date and link."]).strip()
        return {"kind": "document", "title": title, "source_file": rel,
                "summary": f"Reference note {ref_rel}" + (f", linked to {target}" if target else ""),
                "reason": reason, "base_ops": base_ops, "items": items,
                "ops": [*base_ops, *(op for item in items for op in item["ops"])]}

    def compile_new_project(self, vault: Vault, rel: str, name: str, text: str, sha: str, clean: dict[str, Any],
                            today: date, front: dict[str, Any], key: str, start: date | None) -> dict[str, Any]:
        """A project brief: create the project, file the brief as its reference, and offer each action as a step."""
        s = self.settings
        area = link_target(front.get("area"))
        area = area if area in vault.areas else None
        if area and vault.areas[area].private:
            raise ValueError(f"{area} is private; a brief sent to a model cannot create a project there")
        folder = clean["folder"] or f"{s.reference_dir}/DOCUMENTS"
        ref_rel = self.reference_path(folder, name)
        ref_stem = ref_rel.rsplit("/", 1)[-1][:-3]
        title = clean["title"] or Path(name).stem
        outcome = _plain(front.get("outcome") or title, 300)
        due_value = front.get("due")
        try:
            due = due_value if isinstance(due_value, date) else (date.fromisoformat(str(due_value)) if due_value else None)
        except ValueError:
            due = None
        project_rel = f"{s.projects_dir}/{key}/{key}.md"
        note = project_note_text(key, "active", area, due, outcome, today,
                                 notes=["### Source", f"- Created from the Inbox file [[{ref_stem}]]"],
                                 log=f"Project created from the Inbox file [[{ref_stem}]].")
        reference = self.reference_text(text, name, title=title, target=key, doc_type=clean["doc_type"], today=today,
                                        summary=clean["summary"], facts=clean["facts"],
                                        model=s.document_model.rsplit("/", 1)[-1])
        base_ops: list[dict[str, Any]] = [{"op": "create_file", "path": project_rel, "text": note},
                                          {"op": "create_file", "path": ref_rel, "text": reference},
                                          {"op": "remove_file", "path": rel, "sha": sha}]
        items: list[dict[str, Any]] = []
        for deadline in clean["deadlines"]:
            marker = f"gtd-{new_marker()}"
            try:
                line = format_task(deadline["title"], [deadline["context"], "#next"],
                                   dates={"start": start, "due": deadline["due"], "created": today}, block_id=marker)
            except ValueError:
                continue
            items.append({"label": f"Due {_fmt(deadline['due'])} · {deadline['title']} → {key} › Deadlines",
                          "ops": [{"op": "add_task", "path": project_rel, "section": "Deadlines", "position": "end",
                                   "line": line, "marker": f"^{marker}"}]})
        for number, action in enumerate(clean["actions"]):
            marker = f"gtd-{new_marker()}"
            tags = [action["context"]] + (["#next"] if number == 0 else [])
            try:
                line = format_task(action["title"], tags, dates={"start": start, "due": action["due"], "created": today},
                                   block_id=marker)
            except ValueError:
                continue
            label = f"Step · {action['title']}" + (" · #next" if number == 0 else "")
            label += (f" · due {_fmt(action['due'])}" if action["due"] else "") + \
                (f" · shows from {_fmt(start)}" if start else "")
            items.append({"label": f"{label} → {key} › Steps",
                          "ops": [{"op": "add_task", "path": project_rel, "section": "Steps", "position": "queue",
                                   "line": line, "marker": f"^{marker}"}]})
        reason = " ".join([f"Project brief · new active project {key}" + (f" in {area}" if area else "") +
                           f" with {len(items)} item{'s' if len(items) != 1 else ''}.", clean["explanation"],
                           *clean["notes"], "Step 1 carries #next. If you untick it, tag another step #next by hand.",
                           f"{s.document_model} read the whole file; Python checked every date and link."]).strip()
        return {"kind": "document", "title": f"New project {key}", "source_file": rel,
                "summary": f"New project {project_rel}; brief filed as {ref_rel}", "reason": reason,
                "base_ops": base_ops, "items": items, "ops": [*base_ops, *(op for item in items for op in item["ops"])]}
