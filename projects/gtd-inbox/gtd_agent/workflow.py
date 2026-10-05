"""Proposals, the APPROVAL.md review note, approved writes, undo and teacher feedback."""
from __future__ import annotations

import difflib
import html
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .clarification import (ROUTE_LABEL, area_candidates, compile_ops, draft_from_interpretation, ground_split,
                            item_candidates, new_marker, parse_structured, project_candidates, start_control,
                            validate_interpretation)
from .core import (REVIEW_BEGIN, REVIEW_END, SECRET_RE, Capture, Settings, Snapshot, append_capture_lines,
                   atomic_replace, destination_hash, digest, move_file, parse_captures, read_snapshot,
                   remove_capture_line, remove_file, single_writer, snapshot_or_empty, stable_snapshot, write_owned)
from .decisions import (DECIDED_RE, decision_line, decision_sections, decisions_note, open_questions,
                        plain_decision, shown_questions, validate_answer)
from .feedback import BEGIN as FIELD_BEGIN, OUTCOME_HEADING, REASON_HEADING, split_feedback, with_feedback
from . import radicale
from .budget import Budget
from .calendar_ics import event_ics
from .credentials import get_secret
from .drafts import draft_message, shown as shown_draft
from .mailbox import Mailbox, MailError
from .health import Health
from .ledger import Ledger
from .markdown import iter_links
from .ops import OpError, apply_op, describe, expected_line, section_text
from .providers import OpenRouter, ProviderError
from .tasks import TAG_RE, parse_task_line, with_date
from .vault import Vault

PROPOSAL_VERSION = 6
ITEM_RE = re.compile(r"<!-- gtd-agent:(proposal|undo) id=([a-f0-9]{12}) -->")
ITEM_END = "<!-- gtd-agent:item-end -->"
DECISION = "**Decision (choose one):**"
UNDO_DECISION = "**Reverse this change:**"
DONE_SUFFIX = r"(?:\s+(?:✅|❌)\s*\d{4}-\d{2}-\d{2})?"
ITEM_LINE_RE = re.compile(r"^- \[([ xX])\] (\d{1,3})\. ")
# Any checkbox is a control; the block must still match what the agent wrote, so only offered labels can apply.
CONTROL_RE = re.compile(r"^- \[([ xX])\] (\S.*?)" + DONE_SUFFIX + r"\s*$")
HOUSEKEEPING = {"followup", "lint", "promotion"}
UNDO_SHOWN = 30  # every Undo record still available (the ledger keeps 30)
LOG_KEEP = 300
LOG_HEADER = ["# LOG", "", "What the GTD agent changed, newest first. To reverse a recent change, tick its Undo box: it "
              "works while the changed note is still as the agent left it. Rules: [[VAULT_RULES]].", ""]
HISTORY_HEADING = "## History"
APPROVAL_INTRO = ("Check one box per item. The agent applies it within about 10 seconds while it runs. To correct an "
                  "Inbox proposal, write under Your correction, then check Teacher feedback. Applied changes, each "
                  "with its Undo box, are in [[LOG]].")
LOG_INTRO = "Tick Undo to reverse a change. The agent does it within about 10 seconds while it runs."
WRITE_ROOTS_DENIED = (".obsidian", ".trash")


class ApprovalError(RuntimeError):
    pass


def _control(line: str) -> tuple[str, bool] | None:
    match = CONTROL_RE.match(line.strip())
    if not match:
        return None
    return match.group(2), match.group(1).lower() == "x"


def _start_choice(proposal: dict[str, Any]) -> dict[str, Any] | None:
    return next((op["start_choice"] for op in proposal.get("ops", []) if op.get("start_choice")), None)


def _chosen_lead(choice: str, proposal: dict[str, Any]) -> int | None:
    """Days before each due date picked by an 'Approve · show ...' line; None keeps the start as shown."""
    start = _start_choice(proposal)
    if start is None:
        return None
    return next((days for days in start["options"] if start_control(days, start["default"]) == choice), None)


def _normalized_block(body: str) -> str:
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    body, _ = split_feedback(body)
    out = []
    for line in body.split("\n"):
        control = _control(line)
        item = ITEM_LINE_RE.match(line)
        if control:
            out.append(f"- [ ] {control[0]}")
        elif item:
            out.append(f"- [x] {line.rstrip()[6:]}")  # ticking or unticking an item is not an edit
        else:
            out.append(line.rstrip())
    return "\n".join(out).strip()


def _selected_choice(body: str) -> str | None:
    try:
        body, _ = split_feedback(body)
    except ValueError:
        return None
    marker = UNDO_DECISION if UNDO_DECISION in body else DECISION
    if marker not in body:
        return None
    tail = [line for line in body.rsplit(marker, 1)[1].strip().split("\n") if line.strip()]
    controls = [_control(line) for line in tail]
    if not controls or any(control is None for control in controls):
        return None
    checked = [label for label, is_checked in controls if is_checked]  # type: ignore[misc]
    return checked[0] if len(checked) == 1 else None


def _selected_items(body: str) -> list[int]:
    """Numbers of the ticked item lines in a proposal block (before the decision controls)."""
    head = body.split(DECISION, 1)[0]
    return [int(match.group(2)) for line in head.split("\n")
            if (match := ITEM_LINE_RE.match(line.strip())) and match.group(1).lower() == "x"]


def _log_parts(text: str) -> tuple[str | None, list[str]]:
    """(managed Undo block, history lines) of LOG.md. An older LOG with only its list keeps every line."""
    managed = None
    if REVIEW_BEGIN in text and REVIEW_END in text:
        start = text.index(REVIEW_BEGIN)
        end = text.index(REVIEW_END, start) + len(REVIEW_END)
        managed, rest = text[start:end], text[:start] + text[end:]
    else:
        rest = text
    after = rest.split(f"\n{HISTORY_HEADING}\n", 1)[1] if f"\n{HISTORY_HEADING}\n" in rest else rest
    return managed, [line for line in after.split("\n") if line.startswith("- ")]


def _review_text(value: Any) -> str:
    """Render untrusted text as one harmless Markdown line."""
    return str(value).replace("\r", " ").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;").strip()


@dataclass
class ScanResult:
    new_proposals: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    quiet_in: float | None = None  # the Inbox was left unread: seconds until it is quiet. None: it was read


class Agent:
    def __init__(self, settings: Settings, provider: OpenRouter | Any | None = None, today: date | None = None):
        self.settings = settings
        self.ledger = Ledger(settings.state_dir)
        self.health = Health(self.ledger, settings.state_dir)
        self.budget = Budget(self.ledger, settings.budget_monthly_usd, settings.budget_warn_percent)
        self.provider = provider if provider is not None else OpenRouter(settings, budget=self.budget)
        self._today = today
        self._lock_held = False
        self.inbox_quiet_in: float | None = None  # the last scan's ScanResult.quiet_in, for the run loop
        self._inbox_written: tuple[int, float] | None = None  # the agent's last Inbox write: (its file time,
        #                                                        when the user last edited before it)

    # ------------------------------------------------------------ basics
    def today(self) -> date:
        return self._today or date.today()

    def vault(self) -> Vault:
        return Vault(self.settings)

    def close(self) -> None:
        self.ledger.close()

    def _provider_ready(self) -> bool:
        available = getattr(self.provider, "available", None)
        return bool(available()) if callable(available) else True

    def log(self, message: str) -> None:
        """Add a line to LOG's history, newest first (300 kept). The Undo block above it is left exactly as it is,
        ticks included."""
        entry = f"- {datetime.now():%Y-%m-%d %H:%M} {_review_text(message)}"
        for attempt in range(3):
            path = self.settings.vault_path(self.settings.log_note)
            snapshot = read_snapshot(path) if path.exists() else None
            managed, history = _log_parts(snapshot.text if snapshot else "")
            try:
                self._write_log(snapshot, managed, [entry, *history][:LOG_KEEP])
                return
            except RuntimeError:
                if attempt == 2:
                    raise

    def _write_log(self, snapshot: Snapshot | None, managed: str | None, history: list[str]) -> None:
        text = "\n".join([*LOG_HEADER, *([managed, ""] if managed else []), HISTORY_HEADING, *history]) + "\n"
        if snapshot is not None and snapshot.text == text:
            return
        atomic_replace(snapshot.path if snapshot else self.settings.vault_path(self.settings.log_note), text,
                       self.settings.state_dir, snapshot.sha256 if snapshot else None,
                       snapshot.newline if snapshot else "\n", backup=False)

    # ------------------------------------------------------------ write boundary
    def _check_write_path(self, rel: str, kinds: set[str] | None = None) -> Path:
        target = self.settings.vault_path(rel)
        s = self.settings
        kinds = kinds or set()
        allowed = (rel in {s.inbox, s.single_actions, s.someday}
                   or rel.startswith((s.projects_dir + "/", s.areas_dir + "/", s.reviews_dir + "/",
                                      s.agent_dir + "/", s.research_dir + "/")))
        if kinds == {"create_file"} and rel.startswith(s.reference_dir + "/") and not target.exists():
            allowed = True  # a new reference note filed from an Inbox document
        if rel.startswith(s.reference_dir + "/CONTACTS/") and "remove_file" not in kinds:
            allowed = True  # contact notes: the addresses the user confirmed (mail)
        if "remove_file" in kinds:
            allowed = (kinds == {"remove_file"} and s.inbox_dir != "" and rel != s.inbox
                       and rel.rsplit("/", 1)[0] == s.inbox_dir)
        if not allowed or rel.startswith(WRITE_ROOTS_DENIED) or not rel.endswith(".md"):
            raise ApprovalError(f"The agent may not write {rel}")
        return target

    def _check_move(self, op: dict[str, Any]) -> tuple[Path, Path] | None:
        """move_file only takes a PDF from the top of the Inbox folder to a new path under Reference. None when it
        already moved."""
        s = self.settings
        source, dest = op["path"], op["to"]
        allowed = (s.inbox_dir != "" and source.rsplit("/", 1)[0] == s.inbox_dir and source.lower().endswith(".pdf")
                   and dest.startswith(s.reference_dir + "/") and dest.lower().endswith(".pdf")
                   and ".." not in dest.split("/"))
        if not allowed:
            raise ApprovalError(f"The agent may not move {source} to {dest}")
        source_path, dest_path = s.vault_path(source), s.vault_path(dest)
        if not source_path.exists() and destination_hash(dest_path) == op["sha"]:
            return None
        if destination_hash(source_path) != op["sha"]:
            raise OpError("The Inbox file changed since the proposal was made")
        if dest_path.exists():
            raise OpError(f"{dest} already exists")
        return source_path, dest_path

    def calendar_client(self) -> radicale.CalDAV:
        s = self.settings
        if not (s.calendar_enabled and s.calendar_source == "folder"):
            raise ApprovalError("The calendar is off. Set [calendar] enabled = true and source = \"folder\" (Radicale)")
        collection = radicale.choose(radicale.collections(Path(s.calendar_folder), s.calendar_user), s.calendar_name)
        return radicale.CalDAV(s.calendar_url, s.calendar_user, get_secret("radicale", s.state_dir), collection.id)

    def _event_text(self, op: dict[str, Any]) -> str:
        return event_ics(op["uid"], op["summary"], datetime.fromisoformat(op["start"]),
                         datetime.fromisoformat(op["end"]), zone=self.settings.timezone,
                         alert_minutes=int(op["alert_minutes"]), rrule=op.get("rrule"),
                         stamp=datetime.now(timezone.utc).replace(tzinfo=None))

    def apply_ops(self, ops: list[dict[str, Any]], origin_type: str, origin_id: str, summary: str) -> list[str]:
        """Apply ops to current files (and calendar events). All file edits are computed before the first write;
        an event added first is removed again if a file write then fails."""
        today = self.today()
        events = [op for op in ops if op["op"] == "calendar_add"]
        drafts = [op for op in ops if op["op"] == "mail_draft"]
        moves = [(op, found) for op in ops if op["op"] == "move_file" and (found := self._check_move(op))]
        ops = [op for op in ops if op["op"] not in {"calendar_add", "mail_draft", "move_file"}]
        plans: list[tuple[str, Path, Snapshot, str]] = []
        order: list[str] = []
        for op in ops:
            if op["path"] not in order:
                order.append(op["path"])
        for rel in order:
            mine = [op for op in ops if op["path"] == rel]
            target = self._check_write_path(rel, {op["op"] for op in mine})
            snapshot = stable_snapshot(target) if target.exists() else snapshot_or_empty(target)
            if any(op["op"] == "remove_file" for op in mine):
                if snapshot.sha256 is None:
                    continue  # already gone
                if snapshot.sha256 != mine[0]["sha"]:
                    raise OpError("The Inbox file changed since the proposal was made")
                plans.append((rel, target, snapshot, None))
                continue
            text = snapshot.text
            for op in mine:
                text = apply_op(text, op, today)
            plans.append((rel, target, snapshot, text))
        changes: list[dict[str, Any]] = []
        added: list[dict[str, Any]] = []
        client = self.calendar_client() if events else None
        try:
            for op in events:
                result = client.add(op["uid"], self._event_text(op))  # type: ignore[union-attr]
                added.append({"calendar": result["href"], "etag": result["etag"], "summary": op["summary"],
                              "when": op["when"], "name": op["calendar"]})
            for rel, target, snapshot, text in plans:
                if text is None:  # removals go last, after every new copy is written
                    continue
                if text == snapshot.text:
                    continue
                after = atomic_replace(target, text, self.settings.state_dir, snapshot.sha256, snapshot.newline)
                changes.append({"path": rel, "before_text": snapshot.text if snapshot.sha256 else None,
                                "before_hash": snapshot.sha256, "after_hash": after})
                self._advance_watch(rel, snapshot.text, text, [op for op in ops if op["path"] == rel])
            for op, (source, dest) in moves:  # after the note that embeds the file is written
                move_file(source, dest, op["sha"])
                changes.append({"path": op["to"], "moved_from": op["path"], "before_text": None,
                                "before_hash": None, "after_hash": op["sha"]})
            for rel, target, snapshot, text in plans:
                if text is not None:
                    continue
                remove_file(target, self.settings.state_dir, snapshot.sha256)  # type: ignore[arg-type]
                changes.append({"path": rel, "before_text": snapshot.text, "before_hash": snapshot.sha256,
                                "after_hash": None, "newline": snapshot.newline})
        except BaseException:
            for event in added:  # no file changed, or one failed: take the new event back out
                try:
                    client.remove(event["calendar"], event["etag"])  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001 - the original error matters more
                    pass
            raise
        changes += added
        if changes:
            self.ledger.add_undo(origin_type, origin_id, summary, changes)
            self.ledger.expire_undo()
        for op in ops:
            if op["op"] == "cancel_task" and (task := parse_task_line(op["match"], op["path"])) is not None:
                self.note_agent_cancel(task)  # the agent dropped it, so APPROVAL does not ask you why
        if drafts:
            self._save_drafts(drafts)
        return [change.get("path") or change["calendar"] for change in changes]

    def _save_drafts(self, drafts: list[dict[str, Any]]) -> None:
        """Approved drafts go into Proton Drafts through Bridge. A draft that cannot be saved keeps its text in the
        approved proposal; nothing is ever sent."""
        if not (self.settings.mail_enabled and self.settings.mail_source == "bridge"):
            return
        try:
            with Mailbox(self.settings, read_only=True) as box:
                for op in drafts:
                    box.save_draft(draft_message(self.settings, op))
        except MailError as exc:
            self.log(f"CHECK · a draft did not reach your Proton Drafts ({exc}); its text is in the approved proposal")
            return
        self.log(f"Saved {len(drafts)} draft{'s' if len(drafts) != 1 else ''} to your Proton Drafts")

    def _advance_watch(self, rel: str, before: str, after: str, ops: list[dict[str, Any]] | None = None) -> None:
        """The agent's own write is not an edit of yours: move the watched note's baseline past it. With edits of
        yours still unread, the same ops are replayed on the baseline; if they do not fit, both are read later."""
        base = self.ledger.watched(rel)
        if base is None:
            return
        if base == before:
            self.ledger.set_watched(rel, after)
            return
        try:
            for op in ops or []:
                base = apply_op(base, op, self.today())
        except (OpError, ValueError):
            return
        if ops:
            self.ledger.set_watched(rel, base)

    def ops_applied(self, ops: list[dict[str, Any]]) -> bool:
        today = self.today()
        for op in ops:
            kind = op["op"]
            if kind == "mail_draft":
                continue  # best effort: a draft never blocks the change it came with
            if kind == "move_file":
                if self.settings.vault_path(op["path"]).exists() or not self.settings.vault_path(op["to"]).exists():
                    return False
                continue
            if kind == "calendar_add":
                try:
                    client = self.calendar_client()
                    if not client.exists(client.href(op["uid"])):
                        return False
                except (ApprovalError, RuntimeError):
                    return False
                continue
            path = self.settings.vault_path(op["path"])
            text = read_snapshot(path).text if path.exists() else None
            if kind == "remove_file":
                if text is not None:
                    return False
                continue
            if kind == "create_file":
                if text is None:
                    return False
                continue
            if text is None:
                return False
            lines = [line.rstrip() for line in text.split("\n")]
            if kind == "add_task" and op.get("marker") and op["marker"] not in text:
                return False
            if kind in {"complete_task", "cancel_task", "add_tag", "remove_tag", "set_date", "remove_dependency",
                        "replace_line"} and expected_line(op, today).rstrip() not in lines:
                return False
            if kind == "set_property" and f"{op['key']}: {op['value']}".rstrip() not in lines:
                return False
            if kind == "set_section" and section_text(lines, op["section"]) != op["text"].strip():
                return False
            if kind == "append_log" and f"- {op['entry']}" not in lines:
                return False
            if kind == "append_bullet" and f"- {op['text']}" not in lines:
                return False
            if kind == "add_decision" and op["line"].rstrip() not in lines:
                return False
        return True

    # ------------------------------------------------------------ inbox interpretation
    def _manual(self, base: dict[str, Any], reason: str, **extra: Any) -> dict[str, Any]:
        return dict(base, kind="manual", reason=reason, ops=[], **extra)

    def _interpret_single(self, vault: Vault, text: str, capture: Capture, base: dict[str, Any], *,
                          routing: dict | None, reference: date, history: list[str] | None,
                          previous: dict | None, cached: dict | None) -> dict[str, Any]:
        evidence = "\n".join(dict.fromkeys([capture.text, text, *(history or [])]))
        try:
            projects = project_candidates(vault, evidence)
            areas = area_candidates(vault)
            items, lookup = item_candidates(vault, evidence)
            if cached is None:
                examples = self.ledger.learning_examples(evidence)
                value = self.provider.interpret(
                    text, "Jev classification; GLM structured interpretation" if not history
                    else "User corrected the previous interpretation",
                    projects, areas, items, examples, history or None, self._feedback_context(previous or {}),
                    routing=routing, reference_date=reference, capture_checked=capture.checked)
            else:
                value = cached
            value = dict(validate_interpretation(value, evidence, projects, areas, items,
                                                 capture_checked=capture.checked))
        except (ProviderError, ValueError, RuntimeError) as exc:
            return self._manual(base, f"Interpretation needs attention: {exc}",
                                clarification={"original_issue": "Model or validation issue",
                                               "explanation": "No validated change is available. Use Teacher feedback to clarify or retry.",
                                               "assumptions": ""})
        meta = {"interpretation": value, "routing": routing or {}, "reference_date": reference.isoformat()}
        if value["route"] == "needs_clarification":
            return self._manual(dict(base, **meta), f"GLM needs one detail: {value['question']}",
                                clarification={"original_issue": "Missing detail", "explanation": value["explanation"],
                                               "assumptions": value["assumptions"]})
        try:
            draft, extra_assumptions, matched = draft_from_interpretation(vault, value, reference, lookup)
            capture_tags = {tag.casefold() for tag in TAG_RE.findall(text)}
            draft["explicit_next"] = "#next" in capture_tags
            draft["research"] = "#research" in capture_tags
            ops, summary, title = compile_ops(vault, draft, capture=text, reference=reference, marker=new_marker(),
                                              matched_task=matched)
        except (ValueError, RuntimeError) as exc:
            return self._manual(dict(base, **meta), f"Interpretation needs attention: {exc}",
                                clarification={"original_issue": "Validation issue", "explanation": value["explanation"],
                                               "assumptions": value["assumptions"]})
        route = value["route"]
        operation = ("completion_report" if route == "completion_report" else
                     "update_report" if route == "project_note" else "new_capture")
        category = "project" if route == "new_project" else "unclear" if operation != "new_capture" else route
        proposal = dict(base, **meta, kind=route, title=title, summary=summary, ops=ops,
                        reason="GLM interpretation; approval required",
                        clarification={"original_issue": "Jev classification; GLM structured interpretation",
                                       "explanation": value["explanation"],
                                       "assumptions": (value["assumptions"] + " " + extra_assumptions).strip(),
                                       "lesson": value["lesson"]},
                        learning={"capture": text, "operation": operation, "category": category,
                                  "multiplicity": "single", "route": route, "lesson": value["lesson"]})
        if history:
            proposal["teacher_history"] = history
            proposal["learning"]["teacher_feedback"] = history
            proposal["learning"]["corrected_title"] = title
        return proposal

    @staticmethod
    def _feedback_context(proposal: dict) -> dict:
        return {key: proposal[key] for key in ("kind", "title", "summary", "reason") if key in proposal}

    @staticmethod
    def _is_private_text(vault: Vault, text: str) -> bool:
        tags = {tag.casefold() for tag in TAG_RE.findall(text)}
        links = [link.target for link in iter_links(text)]
        return "#private" in tags or any(
            (link in vault.projects and vault.is_private_project(vault.projects[link]))
            or (link in vault.areas and vault.areas[link].private) for link in links)

    def _structured_proposal(self, vault: Vault, text: str, base: dict[str, Any], reference: date,
                             private: bool) -> dict[str, Any] | None:
        try:
            structured = parse_structured(text, vault)
        except ValueError as exc:
            return self._manual(base, f"Explicit capture needs attention: {exc}", explicit=True)
        if structured is None:
            return None
        if structured["unknown_links"]:
            return self._manual(base, "Unknown link: " + ", ".join(structured["unknown_links"])
                                + ". Link an existing project or area key.", explicit=True)
        try:
            draft = dict(structured)
            draft["dates"] = dict(draft["dates"])
            ops, summary, title = compile_ops(vault, draft, capture=text, reference=reference, marker=new_marker())
        except (ValueError, RuntimeError) as exc:
            return self._manual(base, f"Explicit capture needs attention: {exc}", explicit=True)
        return dict(base, kind=structured["route"], title=title, summary=summary, ops=ops,
                    reason="Written in vault syntax, so it was filed without a model call. Approval required",
                    explicit=True, private=private)

    def _decided_proposal(self, vault: Vault, text: str, body: str, base: dict[str, Any], reference: date,
                          private: bool, cached: dict | None = None) -> dict[str, Any]:
        """'Decided: ... [[KEY]]' records a decision, offers to retire the questions it answers, and suggests steps."""
        keys = [link.target for link in iter_links(text) if link.target in vault.projects]
        base = dict(base, explicit=True, private=private, decided=True)
        if len(set(keys)) != 1:
            return self._manual(base, "A decision needs exactly one project link, such as "
                                      "'Decided: Jordan is our advisor [[DEMO_PROJECT]]'.", no_model=True)
        return self.decision_proposal(vault, vault.projects[keys[0]], body, base, reference, private, cached=cached,
                                      capture=text)

    def decision_proposal(self, vault: Vault, project: Any, body: str, base: dict[str, Any], reference: date,
                          private: bool, *, cached: dict | None = None, capture: str = "", record: bool = True,
                          settles: tuple[str, ...] = ()) -> dict[str, Any]:
        """A decision about one project: record it (unless you wrote it already), close the open questions it
        settles and queue the steps it creates. `settles` holds question lines that close whatever GLM says."""
        capture = capture or body
        target, separate = decisions_note(vault, project)
        questions = open_questions(vault, project, target, separate)
        sections = decision_sections(vault.notes[target].text) if separate and target in vault.notes else []
        answer = None
        note = ""
        if cached is not None:
            answer = cached
        elif private:
            note = "Private project: nothing was sent to a model. Tick the questions this answers."
        elif not self.settings.remote_inference or not self._provider_ready():
            note = "Remote inference is off, so the questions were not matched. Tick the ones this answers."
        else:
            try:
                answer = self.provider.decided(body, project.key, [{"id": q["id"], "text": q["text"]} for q in questions],
                                               sections, reference)
            except (ProviderError, ValueError, RuntimeError) as exc:
                note = f"GLM could not match questions ({exc}). Tick the ones this answers."
        clean = None
        if answer is not None:
            try:
                clean = validate_answer(answer, body, questions, sections)
            except ValueError as exc:
                note = f"The model's reading was not used ({exc}). Tick the questions this answers."
        if clean is None:
            decision, why = plain_decision(body)
            clean = {"decision": decision, "why": why, "answered": [], "section": "", "steps": [], "explanation": ""}
        if not clean["decision"]:
            return self._manual(base, "Write the decision after 'Decided:'.", no_model=True)
        forced = [q["id"] for q in questions if q["raw"] in settles]
        clean["answered"] = list(dict.fromkeys([*clean["answered"], *forced]))
        line = decision_line(clean["decision"], clean["why"], reference)
        where = target.rsplit("/", 1)[-1][:-3]
        base_ops = [{"op": "add_decision", "path": target, "line": line,
                     "section": clean["section"] or (None if separate else "Decisions"), "ensure": not separate}]
        base_ops = (base_ops if record else []) + [
            {"op": "append_log", "path": project.rel, "entry": f"{reference.isoformat()} Decided: {clean['decision']}"}]
        items: list[dict[str, Any]] = []
        shown = shown_questions(questions, clean["answered"], body)
        for question in shown:
            if question["tick"]:
                op = {"op": "complete_task", "path": question["path"], "match": question["raw"],
                      "done": reference.isoformat()}
                verb = "Close"
            else:
                op = {"op": "remove_line", "path": question["path"], "match": question["raw"], "occurrence": 1,
                      "note": "answered"}
                verb = "Remove"
            items.append({"label": f"{verb} question ({question['source']}): {question['text'][:160]}",
                          "ops": [op], "checked": question["id"] in clean["answered"]})
        gave_next = False
        for step in clean["steps"]:
            draft = {"route": "next_action", "title": step["title"], "context": step["context"], "project": project,
                     "dates": {}, "extra_tags": [], "force_queue": gave_next}
            try:
                ops, summary, title = compile_ops(vault, draft, capture=capture, reference=reference,
                                                  marker=new_marker())
            except (ValueError, RuntimeError):
                continue
            gave_next = gave_next or any(op.get("position") == "steps" for op in ops)
            items.append({"label": f"Step · {title} → {summary}", "ops": ops, "checked": True})
        matched = sum(1 for item in items if item["checked"] and item["label"].startswith(("Close", "Remove")))
        reason = " ".join(part for part in [
            (f"Decision for {project.key}, recorded in {where}" + (f" › {clean['section']}" if clean["section"] else "")
             if record else f"Decision you wrote in {where}") + ".", clean["explanation"], note,
            f"{matched} of {len(questions)} open question{'s' if len(questions) != 1 else ''} ticked as answered"
            + (f"; the {len(shown) - matched} closest others are listed unticked." if len(shown) > matched else ".")
            if questions else "No open questions found."] if part)
        proposal = dict(base, kind="decision", title=clean["decision"], summary=f"Decision in {where}",
                        reason=reason, base_ops=base_ops, items=items,
                        ops=[*base_ops, *(op for item in items if item["checked"] for op in item["ops"])])
        if answer is not None:
            proposal["decided_answer"] = answer
        return proposal

    def _rebuild(self, vault: Vault, capture: Capture, row: Any) -> dict[str, Any]:
        """Rebuild one stale proposal. Reuses the saved interpretation, so no paid call is repeated."""
        old = json.loads(row["proposal_json"])
        base = {key: old[key] for key in ("source", "parent_source", "item", "reference_date", "group") if key in old}
        base["engine_version"] = PROPOSAL_VERSION
        reference = date.fromisoformat(old.get("reference_date", self.today().isoformat()))
        text = old.get("source", capture.text)
        decided = DECIDED_RE.match(text)
        if old.get("decided") and decided:
            return self._decided_proposal(vault, text, decided.group(1), base, reference, bool(old.get("private")),
                                          cached=old.get("decided_answer"))
        if old.get("explicit"):
            rebuilt = self._structured_proposal(vault, text, base, reference, bool(old.get("private")))
            if rebuilt is not None:
                return rebuilt
        cached = old.get("interpretation") if old.get("kind") != "manual" else None
        if cached is None and (not self.settings.remote_inference or not self._provider_ready()):
            return self._manual(base, "The earlier proposal went stale and cannot be rebuilt without the model")
        proposal = self._interpret_single(vault, text, capture, base, routing=old.get("routing"), reference=reference,
                                          history=old.get("teacher_history"), previous=old, cached=cached)
        if old.get("teacher_history"):
            proposal["teacher_history"] = old["teacher_history"]
        return proposal

    @staticmethod
    def _group_keys(rows: list[Any]) -> list[str]:
        if not rows:
            return []
        latest = max(rows, key=lambda row: (row["updated_at"], row["dedupe_key"]))
        group = json.loads(latest["proposal_json"]).get("group")
        return list(group) if group else [row["dedupe_key"] for row in rows if row["status"] != "stale"] or \
            [latest["dedupe_key"]]

    def _interpret_capture(self, vault: Vault, capture: Capture, previous: dict | None = None
                           ) -> list[tuple[str, dict[str, Any]]]:
        previous = previous or {}
        reference = date.fromisoformat(previous.get("reference_date", self.today().isoformat()))
        base = {"source": capture.text, "reference_date": reference.isoformat(), "engine_version": PROPOSAL_VERSION}
        text = capture.text
        if SECRET_RE.search(text):
            return [(capture.capture_id, self._manual(dict(base, source="[redacted]"),
                                                      "Possible secret in the capture; nothing was sent anywhere",
                                                      no_model=True))]
        private = self._is_private_text(vault, text)
        decided = DECIDED_RE.match(text)
        if decided:
            return [(capture.capture_id, self._decided_proposal(vault, text, decided.group(1), base, reference, private))]
        structured = self._structured_proposal(vault, text, base, reference, private)
        if structured is not None:
            return [(capture.capture_id, structured)]
        if private and "#research" in {tag.casefold() for tag in TAG_RE.findall(text)}:
            return [(capture.capture_id, self._manual(
                base, "Research never runs on private items. Remove #research, or the private tag or link.",
                no_model=True))]
        if private:
            return [(capture.capture_id, self._manual(
                base, "Private capture. Write it in vault syntax so it can be filed without a model: "
                      "add one context tag (#computer, #calls, #anywhere, #errands), or use "
                      "#waiting Person: what, or #someday.", no_model=True))]
        if not self.settings.remote_inference:
            return [(capture.capture_id, self._manual(
                base, "Remote inference is off. Add a context tag or #waiting Person: what to file it without a model.",
                no_model=True, retry=True))]
        if not self._provider_ready():
            return [(capture.capture_id, self._manual(base, "OpenRouter key is missing", no_model=True, retry=True))]
        try:
            routing = previous.get("routing") or self.provider.decide(text, self.ledger.learning_examples(text))
        except ProviderError as exc:
            return [(capture.capture_id, self._manual(base, f"Routing failed: {exc}", retry=True))]
        multiplicity = routing.get("multiplicity", ("single", 0.0)) if isinstance(routing, dict) else ("single", 0)
        if multiplicity and multiplicity[0] == "multiple" and not previous.get("teacher_history"):
            try:
                parts = ground_split(text, self.provider.split(text, routing))
            except (ProviderError, ValueError):
                parts = [text]
            if len(parts) > 1:
                results = []
                for index, part in enumerate(parts, 1):
                    child_base = dict(base, source=part, parent_source=text, item={"index": index, "count": len(parts)})
                    child_routing = dict(routing, multiplicity=["single", 1.0])
                    results.append((f"{capture.capture_id}#{index}", self._interpret_single(
                        vault, part, capture, child_base, routing=child_routing, reference=reference,
                        history=None, previous=None, cached=None)))
                return results
        return [(capture.capture_id, self._interpret_single(
            vault, text, capture, base, routing=routing, reference=reference,
            history=previous.get("teacher_history"), previous=previous, cached=previous.get("interpretation")))]

    # ------------------------------------------------------------ scanning
    @single_writer
    def append_to_inbox(self, lines: list[str]) -> None:
        """Captures from outside the vault (Telegram, mail the user writes to himself), one Inbox line each."""
        snapshot = stable_snapshot(self.settings.vault_path(self.settings.inbox))
        self._write_inbox(snapshot, append_capture_lines(snapshot, lines))

    def _inbox_edited(self, path: Path) -> float:
        """When the user last changed the Inbox. The agent's own writes to it are not his edits."""
        stat = path.stat()
        written = self._inbox_written
        return written[1] if written and written[0] == stat.st_mtime_ns else stat.st_mtime

    def _write_inbox(self, snapshot: Snapshot, text: str) -> None:
        """The agent's own change to the Inbox: adding a capture, removing an approved line."""
        edited = self._inbox_edited(snapshot.path)
        atomic_replace(snapshot.path, text, self.settings.state_dir, snapshot.sha256, snapshot.newline)
        self._inbox_written = (snapshot.path.stat().st_mtime_ns, edited)

    def _inbox_quiet_in(self, path: Path) -> float | None:
        """Seconds until the Inbox is quiet: untouched by the user inbox_quiet_seconds, so a line he is still typing is
        not read half-written. None once it is, or when the file's time is ahead of the clock (a synced edit)."""
        quiet = self.settings.inbox_quiet_seconds
        untouched = time.time() - self._inbox_edited(path)
        return quiet - untouched if 0 <= untouched < quiet else None

    @single_writer
    def scan(self, wait_for_quiet: bool = True) -> ScanResult:
        """Read the Inbox once it is quiet and queue a proposal for each new line."""
        result = ScanResult()
        self.inbox_quiet_in = None
        inbox_path = self.settings.vault_path(self.settings.inbox)
        if not inbox_path.exists():
            raise RuntimeError(f"Inbox note is missing: {self.settings.inbox}")
        if wait_for_quiet:
            result.quiet_in = self.inbox_quiet_in = self._inbox_quiet_in(inbox_path)
        if result.quiet_in is None:
            self._read_inbox(inbox_path, result)
        if self.settings.mode == "approval":
            result.errors += [outcome for outcome in self.apply_trusted() if not outcome.startswith("Applied")]
            self.render_reviews()
        return result

    def _read_inbox(self, inbox_path: Path, result: ScanResult) -> None:
        """One proposal per new line. A proposal whose line is gone or reworded goes stale; a line whose proposals
        are all applied is removed."""
        inbox = stable_snapshot(inbox_path)
        captures = parse_captures(inbox, self.settings.max_capture_chars)
        active = {capture.capture_id for capture in captures}
        for row in self.ledger.list("pending", "inbox"):
            if row["capture_id"] not in active:
                self.ledger.set_status(row["id"], "stale")
        counts: dict[str, int] = {}
        for capture in captures:
            counts[capture.text] = counts.get(capture.text, 0) + 1
        vault = None
        for capture in captures:
            rows = self.ledger.for_capture(capture.capture_id)
            if rows and self.ledger.was_cleared(capture.capture_id):
                self.ledger.forget_cleared(capture.capture_id)  # the same words were captured again
                rows = []
            previous: dict[str, Any] = {}
            if rows:
                if any(row["status"] == "applying" for row in rows):
                    result.skipped += 1
                    continue
                group = self._group_keys(rows)
                members = [row for row in rows if row["dedupe_key"] in group]
                same = bool(members) and all(row["source_hash"] == capture.source_sha256 for row in members)
                current = all(json.loads(row["proposal_json"]).get("engine_version") == PROPOSAL_VERSION
                              for row in members)
                retry = (len(members) == 1 and members[0]["status"] == "pending"
                         and json.loads(members[0]["proposal_json"]).get("retry")
                         and datetime.fromisoformat(members[0]["updated_at"]) < datetime.now() - timedelta(minutes=10)
                         and self.settings.remote_inference and self._provider_ready())
                if same and current and len(members) == len(group) and not retry:
                    stale = [row for row in members if row["status"] == "stale"]
                    if not stale:
                        if all(row["status"] == "committed" for row in members):
                            try:
                                self._clear_capture(capture.capture_id, capture.source_sha256, capture.text)
                            except (OSError, RuntimeError) as exc:
                                result.errors.append(f"Inbox line {capture.line_number}: {exc}")
                        result.skipped += 1
                        continue
                    if vault is None:
                        vault = self.vault()
                    for row in stale:
                        proposal = self._rebuild(vault, capture, row)
                        proposal["group"] = group
                        proposal["line_number"] = capture.line_number
                        proposal_id = self.ledger.upsert("inbox", row["dedupe_key"], proposal,
                                                         capture_id=capture.capture_id,
                                                         source_hash=capture.source_sha256)
                        result.new_proposals.append({"id": proposal_id, "proposal": proposal})
                    continue
                if len(members) == 1:
                    old = json.loads(members[0]["proposal_json"])
                    previous = {key: old[key] for key in ("teacher_history", "reference_date") if key in old}
            if vault is None:
                vault = self.vault()
            try:
                if counts[capture.text] > 1:
                    items = [(capture.capture_id, {
                        "kind": "manual", "ops": [], "source": capture.text, "engine_version": PROPOSAL_VERSION,
                        "reference_date": self.today().isoformat(),
                        "reason": "Identical Inbox lines need distinct wording before processing"})]
                else:
                    items = self._interpret_capture(vault, capture, previous)
            except (OSError, RuntimeError, ValueError) as exc:
                result.errors.append(f"Inbox line {capture.line_number}: {exc}")
                continue
            keys = [key for key, _ in items]
            for dedupe_key, proposal in items:
                proposal["group"] = keys
                proposal["line_number"] = capture.line_number
                prior = self.ledger.get_by_key(dedupe_key)
                if prior is not None and prior["status"] == "pending":
                    carried = self._draft_feedback(prior["id"])
                    if carried:
                        proposal["carried_feedback"] = carried
                if self.settings.mode == "approval":
                    proposal_id = self.ledger.upsert("inbox", dedupe_key, proposal, capture_id=capture.capture_id,
                                                     source_hash=capture.source_sha256)
                else:
                    proposal_id = "PREVIEW"
                result.new_proposals.append({"id": proposal_id, "proposal": proposal})
            for row in rows:
                if row["dedupe_key"] not in keys and row["status"] == "pending":
                    self.ledger.set_status(row["id"], "stale")

    def _clear_capture(self, capture_id: str, source_hash: str, source_text: str) -> None:
        rows = self.ledger.for_capture(capture_id)
        group = self._group_keys(rows)
        members = [row for row in rows if row["dedupe_key"] in group]
        if not members or len(members) != len(group) or any(row["status"] != "committed" for row in members):
            return
        path = self.settings.vault_path(self.settings.inbox)
        snapshot = stable_snapshot(path)
        updated = remove_capture_line(snapshot, capture_id, source_hash, source_text, self.settings.max_capture_chars)
        if updated is not None:
            self._write_inbox(snapshot, updated)
        self.ledger.mark_cleared(capture_id)

    # ------------------------------------------------------------ decisions
    @single_writer
    def approve(self, proposal_id: str, render: bool = True, selected: list[int] | None = None,
                start_lead: int | None = None) -> str:
        if self.settings.mode != "approval":
            raise ApprovalError("Switch config.toml to approval mode before approving")
        row = self.ledger.get(proposal_id)
        if row is None or row["status"] != "pending":
            raise ApprovalError("Proposal is missing or no longer pending")
        proposal = json.loads(row["proposal_json"])
        if proposal.get("engine_version") != PROPOSAL_VERSION:
            raise ApprovalError("Proposal predates this agent version; rescan for a fresh preview")
        if proposal["kind"] == "manual" or not proposal.get("ops"):
            raise ApprovalError("Nothing to apply. Write a correction and check Teacher feedback")
        if row["origin"] == "inbox":
            inbox = stable_snapshot(self.settings.vault_path(self.settings.inbox))
            capture = next((c for c in parse_captures(inbox, self.settings.max_capture_chars)
                            if c.capture_id == row["capture_id"]), None)
            if capture is None or capture.source_sha256 != row["source_hash"]:
                self.ledger.set_status(proposal_id, "stale")
                raise ApprovalError("The Inbox line changed; the next scan makes a fresh proposal")
        label = ROUTE_LABEL.get(proposal["kind"], proposal["kind"].replace("_", " ").capitalize())
        if proposal.get("items"):
            if selected is None:
                selected = [n for n, item in enumerate(proposal["items"], 1) if item.get("checked", True)]
            chosen = [item for number, item in enumerate(proposal["items"], 1) if number in set(selected)]
            proposal["ops"] = [*proposal.get("base_ops", []), *(op for item in chosen for op in item["ops"])]
            proposal["summary"] = (proposal.get("summary", "") +
                                   f" · {len(chosen)} of {len(proposal['items'])} items").strip(" ·")
            self.ledger.update_json(proposal_id, proposal)
        for op in proposal["ops"]:
            choice = op.get("start_choice")
            if choice and start_lead is not None and start_lead in choice["options"]:
                start = date.fromisoformat(choice["due"]) - timedelta(days=start_lead)
                op["line"] = with_date(op["line"], "start", start)
                self.ledger.update_json(proposal_id, proposal)
        self.ledger.set_status(proposal_id, "applying")
        try:
            written = self.apply_ops(proposal["ops"], "proposal", proposal_id,
                                     f"{label}: {proposal.get('title') or proposal.get('summary', '')}")
        except OpError as exc:
            self.ledger.set_status(proposal_id, "stale")
            raise ApprovalError(f"Not applied: {exc}. The next scan rebuilds the proposal") from exc
        except (ApprovalError, RuntimeError, OSError, ValueError) as exc:
            self.ledger.set_status(proposal_id, "pending")  # e.g. a sync lock; the next poll retries
            raise ApprovalError(f"Not applied yet: {exc}") from exc
        self.ledger.set_status(proposal_id, "committed")
        self.ledger.event("gtd.proposal_committed", {"id": proposal_id, "origin": row["origin"],
                                                     "kind": proposal["kind"], "paths": written})
        verb = "Auto · trusted" if proposal.get("trusted") else "Approved ·"
        self.log(f"{verb} {label}: {proposal.get('title', '')} → {proposal.get('summary', '')}")
        message = f"Applied {proposal['kind']} ({proposal.get('summary', '')})"
        if row["origin"] == "inbox":
            try:
                self._clear_capture(row["capture_id"], row["source_hash"], proposal.get("parent_source")
                                    or proposal.get("source", ""))
            except (OSError, RuntimeError) as exc:
                message += f"; Inbox line needs manual cleanup ({exc})"
        if render:
            self.render_reviews()
        return message

    @single_writer
    def apply_trusted(self) -> list[str]:
        """Proposal kinds you moved to bookkeeping ([trust] bookkeeping) apply at once, logged and undoable. A
        trusted apply is not your confirmation, so it saves no learned example."""
        if not self.settings.trust_bookkeeping or self.settings.mode != "approval":
            return []
        outcomes = []
        for row in self.ledger.pending():
            proposal = json.loads(row["proposal_json"])
            if proposal.get("kind") not in self.settings.trust_bookkeeping or not proposal.get("ops"):
                continue
            proposal.pop("learning", None)
            proposal["trusted"] = True
            self.ledger.update_json(row["id"], proposal)
            try:
                outcomes.append(self.approve(row["id"], render=False))
            except (ApprovalError, OSError, ValueError, RuntimeError) as exc:
                outcomes.append(f"{row['id']}: {exc}")
        return outcomes

    def answer(self, proposal_id: str, label: str, render: bool = True) -> str:
        """Apply the answer you ticked on a vault choice. Leave it stops asking until the problem changes."""
        row = self.ledger.get(proposal_id)
        if row is None or row["status"] != "pending":
            raise ApprovalError("Proposal is missing or no longer pending")
        proposal = json.loads(row["proposal_json"])
        if label == "Leave it":
            self.reject(proposal_id, render=render, status="left")
            self.log(f"Left as is · {proposal.get('title', '')}")
            return f"Left {proposal_id} as is"
        picked = next((choice for choice in proposal.get("choices", []) if choice["label"] == label), None)
        if picked is None:
            raise ApprovalError(f"'{label}' is not an answer to this question")
        if picked.get("consent"):  # an answer that changes no file: the agent acts on it at its next scan
            proposal.update(summary=label)
            self.ledger.update_json(proposal_id, proposal)
            self.ledger.set_status(proposal_id, "committed")
            self.log(f"Answered · {proposal.get('title', '')} → {label}")
            if render:
                self.render_reviews()
            return f"Answered {proposal_id}: {label}"
        proposal.update(ops=picked["ops"], summary=label)
        self.ledger.update_json(proposal_id, proposal)
        return self.approve(proposal_id, render=render)

    def reject(self, proposal_id: str, render: bool = True, status: str = "rejected") -> None:
        row = self.ledger.get(proposal_id)
        if row is None or row["status"] != "pending":
            raise ApprovalError("Proposal is missing or no longer pending")
        self.ledger.set_status(proposal_id, status)
        if render and self.settings.mode == "approval":
            self.render_reviews()

    @single_writer
    def reconcile(self) -> list[str]:
        outcomes = []
        for row in self.ledger.list("applying"):
            proposal = json.loads(row["proposal_json"])
            if self.ops_applied(proposal.get("ops", [])):
                self.ledger.set_status(row["id"], "committed")
                if row["origin"] == "inbox":
                    try:
                        self._clear_capture(row["capture_id"], row["source_hash"],
                                            proposal.get("parent_source") or proposal.get("source", ""))
                    except (OSError, RuntimeError) as exc:
                        outcomes.append(f"{row['id']}: applied; Inbox needs manual cleanup ({exc})")
                        continue
                outcomes.append(f"{row['id']}: change confirmed")
            else:
                self.ledger.set_status(row["id"], "stale")
                outcomes.append(f"{row['id']}: change not found; marked stale for a fresh proposal")
        return outcomes

    # ------------------------------------------------------------ drop reasons
    def note_agent_cancel(self, task: Any) -> None:
        """The agent cancelled this task itself, so no drop question follows."""
        self.ledger.kv_set(f"agentcancel|{digest(f'{task.path}{chr(10)}{task.norm}')[:20]}", datetime.now().isoformat())

    @single_writer
    def save_drop_reason(self, proposal_id: str, reason: str, render: bool = True) -> str:
        """Record why a task was dropped: a Decisions line and a Log line where the task lived. Undoable."""
        row = self.ledger.get(proposal_id)
        if row is None or row["status"] != "pending":
            raise ApprovalError("This question is no longer open")
        proposal = json.loads(row["proposal_json"])
        if proposal.get("kind") != "drop_reason":
            raise ApprovalError("Only a drop question takes a reason")
        why = " ".join(reason.split()).strip().rstrip(".")
        if not why or len(why) > 300 or "<!--" in why:
            raise ApprovalError("Write the reason in one line (up to 300 characters) under Your reason, then tick "
                                "Save reason")
        drop = proposal["drop"]
        day = date.fromisoformat(drop["date"])
        proposal["ops"] = [
            {"op": "add_decision", "path": drop["decisions"], "line": decision_line(f"Dropped: {drop['title']}", why, day),
             "section": drop["section"], "ensure": drop["ensure"]},
            {"op": "append_log", "path": drop["log"], "entry": f"{day.isoformat()} Dropped: {drop['title']} · {why}"}]
        self.ledger.update_json(proposal_id, proposal)
        self.ledger.set_status(proposal_id, "applying")
        try:
            self.apply_ops(proposal["ops"], "proposal", proposal_id, f"Why drop: {drop['title']}")
        except OpError as exc:
            self.ledger.set_status(proposal_id, "pending")
            raise ApprovalError(f"Not recorded: {exc}") from exc
        except (ApprovalError, RuntimeError, OSError, ValueError) as exc:
            self.ledger.set_status(proposal_id, "pending")
            raise ApprovalError(f"Not recorded yet: {exc}") from exc
        self.ledger.set_status(proposal_id, "committed")
        self.log(f"Dropped · {drop['title']}: {why}")
        if render:
            self.render_reviews()
        return f"Dropped: {drop['title']} (reason recorded in {drop['where']})"

    @single_writer
    def save_event_outcome(self, proposal_id: str, text: str, render: bool = True) -> str:
        """What came out of a project's event: a Log line now. GLM's steps follow as a proposal at the next scan
        (not for a private project)."""
        row = self.ledger.get(proposal_id)
        if row is None or row["status"] != "pending":
            raise ApprovalError("This question is no longer open")
        proposal = json.loads(row["proposal_json"])
        if proposal.get("kind") != "after_event":
            raise ApprovalError("Only an event question takes this answer")
        line = " ".join(text.split()).strip()
        if not line or len(line) > 300 or "<!--" in line or SECRET_RE.search(line):
            raise ApprovalError("Write what came out of it in one line (up to 300 characters, no passwords) under "
                                "What came out of it, then tick Save")
        event = proposal["event"]
        proposal["answer"] = line
        proposal["ops"] = [{"op": "append_log", "path": event["rel"], "entry": f"{event['day']} {event['title']}: {line}"}]
        self.ledger.update_json(proposal_id, proposal)
        self.ledger.set_status(proposal_id, "applying")
        try:
            self.apply_ops(proposal["ops"], "proposal", proposal_id, f"After {event['title']}")
        except OpError as exc:
            self.ledger.set_status(proposal_id, "pending")
            raise ApprovalError(f"Not saved: {exc}") from exc
        except (ApprovalError, RuntimeError, OSError, ValueError) as exc:
            self.ledger.set_status(proposal_id, "pending")
            raise ApprovalError(f"Not saved yet: {exc}") from exc
        self.ledger.set_status(proposal_id, "committed")
        if not event.get("private"):
            self.ledger.kv_set(f"aftersteps|{proposal_id}", "waiting")
        self.log(f"Saved · what came out of {event['title']} ({event['project']}): {line}")
        if render:
            self.render_reviews()
        return f"Saved what came out of {event['title']} in {event['project']}'s Log"

    # ------------------------------------------------------------ undo
    @single_writer
    def undo(self, undo_id: str, render: bool = True) -> str:
        record = self.ledger.get_undo(undo_id)
        if record is None or record["status"] != "available":
            raise ApprovalError("Undo is missing or no longer available")
        changes = json.loads(record["changes_json"])
        events = [change for change in changes if "calendar" in change]
        targets = [(change, self.settings.vault_path(change["path"])) for change in changes if "path" in change]
        if any(destination_hash(target) != change["after_hash"] for change, target in targets):
            self.ledger.undo_status(undo_id, "stale")
            raise ApprovalError("A file changed since then; Undo stopped without changing anything")
        try:
            client = self.calendar_client() if events else None
            for event in events:
                if client.remove(event["calendar"], event["etag"]) == "changed":  # type: ignore[union-attr]
                    self.ledger.undo_status(undo_id, "stale")
                    raise ApprovalError("The event was changed on your calendar since; Undo stopped. Delete it on "
                                        "your phone if you want it gone")
        except radicale.CalendarError as exc:
            raise ApprovalError(f"Undo waits: {exc}") from exc
        try:
            for change, target in targets:
                if change.get("moved_from"):  # a filed PDF goes back to the Inbox folder
                    move_file(target, self.settings.vault_path(change["moved_from"]), change["after_hash"])
                elif change["before_text"] is None:
                    recovery = self.settings.state_dir / "undo_trash" / undo_id / change["path"]
                    recovery.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(recovery))
                    folder = target.parent
                    projects = self.settings.vault_path(self.settings.projects_dir)
                    while folder != projects and projects in folder.parents:
                        try:
                            folder.rmdir()
                        except OSError:
                            break
                        folder = folder.parent
                else:
                    current = read_snapshot(target) if target.exists() else None
                    newline = current.newline if current else change.get("newline", "\n")
                    atomic_replace(target, change["before_text"], self.settings.state_dir, change["after_hash"],
                                   newline)
                    if current is not None:
                        self._advance_watch(change["path"], current.text, change["before_text"])
        except (OSError, RuntimeError) as exc:
            self.ledger.undo_status(undo_id, "interrupted")
            raise ApprovalError(f"Undo interrupted; check agent backups ({exc})") from exc
        self.ledger.undo_status(undo_id, "undone")
        self.ledger.event("gtd.change_undone", {"undo_id": undo_id, "origin": record["origin_id"]})
        self.log(f"Undone · {record['summary']}")
        if render:
            self.render_reviews()
        return f"Reversed: {record['summary']}"

    # ------------------------------------------------------------ teacher feedback
    def _draft_feedback(self, proposal_id: str) -> str:
        path = self.settings.vault_path(self.settings.review)
        if not path.exists():
            return ""
        text = read_snapshot(path).text
        marker = f"<!-- gtd-agent:proposal id={proposal_id} -->"
        if marker not in text:
            return ""
        body = text.split(marker, 1)[1].split(ITEM_END, 1)[0].strip()
        try:
            return split_feedback(body)[1]
        except ValueError:
            return ""

    def _feedback_still_selected(self, proposal_id: str, feedback: str, canonical: str) -> bool:
        current = read_snapshot(self.settings.vault_path(self.settings.review)).text
        if REVIEW_BEGIN not in current or REVIEW_END not in current:
            return False
        chunks = ITEM_RE.split(current.split(REVIEW_BEGIN, 1)[1].split(REVIEW_END, 1)[0])
        for index in range(1, len(chunks), 3):
            if chunks[index:index + 2] != ["proposal", proposal_id]:
                continue
            if ITEM_END not in chunks[index + 2]:
                return False
            body = chunks[index + 2].split(ITEM_END, 1)[0].strip()
            try:
                _, now_feedback = split_feedback(body)
                return (now_feedback == feedback and _selected_choice(body) == "Teacher feedback"
                        and _normalized_block(body) == _normalized_block(canonical))
            except ValueError:
                return False
        return False

    @single_writer
    def submit_teacher_feedback(self, proposal_id: str, feedback: str, canonical: str) -> str:
        row = self.ledger.get(proposal_id)
        if row is None or row["status"] != "pending" or row["origin"] != "inbox":
            raise ApprovalError("Teacher feedback needs a pending Inbox proposal")
        if not self.settings.remote_inference or self.settings.mode != "approval":
            raise ApprovalError("Teacher feedback needs approval mode and remote inference")
        if not self._provider_ready():
            raise ApprovalError("OpenRouter key is missing")
        if not feedback.strip() or len(feedback) > 4000:
            raise ApprovalError("Write a correction of 1 to 4000 characters")
        old = json.loads(row["proposal_json"])
        if old.get("explicit") or old.get("private"):
            raise ApprovalError("This capture was filed from vault syntax; edit the Inbox line instead")
        inbox = stable_snapshot(self.settings.vault_path(self.settings.inbox))
        capture = next((c for c in parse_captures(inbox, self.settings.max_capture_chars)
                        if c.capture_id == row["capture_id"]), None)
        if capture is None or capture.source_sha256 != row["source_hash"]:
            raise ApprovalError("The Inbox line changed; scan before submitting feedback")
        history = [*old.get("teacher_history", []), feedback]
        if len("\n".join(history)) > 24000:
            raise ApprovalError("Correction history is too long; summarize it in a new Inbox line")
        if SECRET_RE.search("\n".join([capture.text, *history])):
            raise ApprovalError("Possible secret in the capture or feedback; nothing sent")
        request_hash = digest(row["proposal_json"] + "\n" + feedback)
        request = self.ledger.feedback_request(proposal_id, request_hash)
        if request is not None:
            if request["status"] != "ready":
                return ""  # a held checkbox is not a new paid request
            request_id = request["id"]
            revised = json.loads(request["result_json"])
        else:
            request_id = self.ledger.begin_feedback(proposal_id, request_hash)
            try:
                base = {key: old[key] for key in ("source", "parent_source", "item", "reference_date") if key in old}
                base["engine_version"] = PROPOSAL_VERSION
                reference = date.fromisoformat(old.get("reference_date", self.today().isoformat()))
                revised = self._interpret_single(self.vault(), old.get("source", capture.text), capture, base,
                                                 routing=old.get("routing"), reference=reference, history=history,
                                                 previous=old, cached=None)
                revised["teacher_history"] = history
                if old.get("group"):
                    revised["group"] = old["group"]
                self.ledger.finish_feedback(request_id, "ready", "Correction processed; revised preview below.", revised)
            except (OSError, ValueError, RuntimeError) as exc:
                message = f"Feedback could not be processed: {exc}. Edit the correction and check Teacher feedback to retry."
                self.ledger.finish_feedback(request_id, "failed", message)
                return f"{proposal_id}: {message}"
        current = stable_snapshot(self.settings.vault_path(self.settings.inbox))
        unchanged = any(c.capture_id == capture.capture_id and c.source_sha256 == capture.source_sha256
                        for c in parse_captures(current, self.settings.max_capture_chars))
        if not unchanged or not self._feedback_still_selected(proposal_id, feedback, canonical):
            return f"{proposal_id}: feedback or Inbox line changed while GLM worked; your latest edits were kept"
        revised_id = self.ledger.replace_after_feedback(proposal_id, request_id, revised)
        return f"{proposal_id}: revised proposal {revised_id} is ready. Approve it to apply the change and save the lesson."

    # ------------------------------------------------------------ APPROVAL.md
    def _proposal_block(self, row: Any) -> str:
        proposal = json.loads(row["proposal_json"])
        kind = proposal["kind"]
        today = self.today()
        heading = _review_text(proposal.get("title") or proposal.get("source") or kind)
        label = ROUTE_LABEL.get(kind, kind)
        lines = [f"### {label} · {heading}", f"ID: `{row['id']}` ^p-{row['id']}"]
        if proposal.get("source"):
            lines.append(f"Source: {_review_text(proposal['source'])}")
        if proposal.get("item"):
            item = proposal["item"]
            lines.append(f"Part {item['index']} of {item['count']} from: {_review_text(proposal.get('parent_source', ''))}")
        if proposal.get("summary"):
            lines.append(f"Where: {_review_text(proposal['summary'])}")
        lines.append(f"Why: {_review_text(proposal.get('reason', 'Review required'))}")
        clarification = proposal.get("clarification")
        if clarification:
            lines.append(f"Interpretation: {_review_text(clarification.get('explanation', ''))}")
            lines.append(f"Assumptions: {_review_text(clarification.get('assumptions') or 'None stated')}")
            if proposal.get("learning"):
                lines.append(f"Lesson saved on approval: {_review_text(clarification.get('lesson', ''))}")
        for number, feedback in enumerate(proposal.get("teacher_history", []), 1):
            lines.append(f"Teacher correction {number}: {_review_text(feedback)}")
        request = self.ledger.latest_feedback(row["id"])
        if request:
            lines.append(f"Feedback status: {_review_text(request['message'])}")
        ops = proposal.get("ops", [])
        items = proposal.get("items") or []
        if items:
            lines += ["", "Items (untick any you don't want, then Approve):",
                      *[f"- [{'x' if item.get('checked', True) else ' '}] {number}. {_review_text(item['label'])}"
              for number, item in enumerate(items, 1)]]
            ops = proposal.get("base_ops", ops)
        for draft in proposal.get("drafts") or []:
            lines += ["", f"Draft · {_review_text(draft['title'])} · To: {_review_text(draft.get('to') or 'add the address')}"
                          f" · Subject: {_review_text(draft['subject'])}", "```text", *shown_draft(draft["body"]), "```",
                      "Saved to your Proton Drafts when you approve; you send it." if draft.get("saved")
                      else "Paste it into Proton; you send it."]
        if kind == "lint_choice":
            lines += ["Now:", f"<pre>{html.escape(proposal.get('preview', ''))}</pre>"]
        elif kind == "contact" or proposal.get("choices"):
            preview = "\n\n".join(f"{choice['label']}:\n" + ("\n".join(describe(op, today) for op in choice["ops"])
                                                              or choice.get("note", ""))
                                  for choice in proposal.get("choices", []))
            lines += ["Change, by answer:", f"<pre>{html.escape(preview)}</pre>"]
        elif kind == "drop_reason":
            drop = proposal["drop"]
            lines.append(f"Change: your reason goes to {_review_text(drop['where'])} as `- **Dropped: "
                         f"{_review_text(drop['title'])}** · your reason · {drop['date']}`, plus a Log line. "
                         "Skip if it needs no record.")
        elif kind == "after_event":
            event = proposal["event"]
            lines.append(f"Change: your line goes to {event['project']}'s Log as `- {event['day']} "
                         f"{_review_text(event['title'])}: your line`. Skip if nothing came out of it.")
        elif ops:
            try:
                preview = "\n\n".join(describe(op, today) for op in ops)
            except Exception as exc:  # a preview must never break the review note
                preview = f"(preview unavailable: {exc})"
            if row["origin"] == "inbox" and row["capture_id"]:
                source = proposal.get("parent_source") or proposal.get("source", "")
                preview += f"\n\nInbox line cleared after approval: {source}"
            lines += ["Change:", f"<pre>{html.escape(preview)}</pre>"]
        elif not items:  # with items, the boxes above are the change
            lines.append("Change: none yet. Correct the interpretation below, or edit the Inbox line.")
        controls = self._controls(row["origin"], proposal)
        if any(label.startswith("Approve · show") for label in controls):
            lines += ["", "When should each copy start showing? The line above has the default start (🛫). Tick "
                          "the Approve line you want."]
        lines += ["", DECISION, *[f"- [ ] {label}" for label in controls]]
        body = "\n".join(lines)
        if "Teacher feedback" in controls:
            return with_feedback(body, proposal.get("carried_feedback", ""))
        if kind == "drop_reason":
            return with_feedback(body, "", heading=REASON_HEADING)
        if kind == "after_event":
            return with_feedback(body, "", heading=OUTCOME_HEADING)
        return body

    def _controls(self, origin: str, proposal: dict[str, Any]) -> list[str]:
        if proposal.get("choices"):
            return [choice["label"] for choice in proposal["choices"]] + ["Leave it"]
        if proposal.get("kind") == "drop_reason":
            return ["Save reason", "Skip"]
        if proposal.get("kind") == "after_event":
            return ["Save", "Skip"]
        if origin != "inbox":
            return (["Approve"] if proposal.get("ops") else []) + ["Reject", "Set aside"]
        teachable = not proposal.get("explicit") and not proposal.get("no_model")
        start = _start_choice(proposal)
        controls = [] if not proposal.get("ops") else \
            [start_control(days, start["default"]) for days in start["options"]] if start else ["Approve"]
        controls.append("Reject")
        if teachable:
            controls.append("Teacher feedback")
        return controls

    def _undo_block(self, record: Any) -> str:
        preview = []
        for change in json.loads(record["changes_json"]):
            if "calendar" in change:
                preview.append(f"Calendar {change.get('name', '')}: removes {change.get('summary', '')} "
                               f"({change.get('when', '')}), unless it was changed on your phone since")
                continue
            target = self.settings.vault_path(change["path"])
            stale = destination_hash(target) != change["after_hash"]
            if change.get("moved_from"):
                detail = f"Moves back to {change['moved_from']}."
            elif change["before_text"] is None:
                detail = "Created file moves to recoverable agent storage."
            elif change["after_hash"] is None:
                detail = "Removed file is restored."
            else:
                current = read_snapshot(target).text if target.exists() else ""
                detail = "".join(difflib.unified_diff(
                    current.splitlines(keepends=True), change["before_text"].splitlines(keepends=True),
                    fromfile=f"now/{change['path']}", tofile=f"restored/{change['path']}", n=1))
            preview.append(f"{change['path']}" + (" · CHANGED SINCE (undo blocked)" if stale else "")
                           + "\n" + (detail or "(no text difference)"))
        return "\n".join([
            f"### Undo · {_review_text(record['summary'])}",
            f"ID: `{record['id']}` · {record['created_at'][:16].replace('T', ' ')}",
            "<details><summary>Show what Undo restores</summary>",
            f"<pre>{html.escape(chr(10).join(preview))}</pre>", "</details>",
            "", UNDO_DECISION, "- [ ] Undo"])

    def _expected_blocks(self) -> dict[tuple[str, str], tuple[str, str]]:
        blocks: dict[tuple[str, str], tuple[str, str]] = {}
        rows = sorted(self.ledger.pending(), key=lambda r: (
            json.loads(r["proposal_json"]).get("line_number", 0), r["created_at"], r["dedupe_key"]))
        for row in rows:
            group = ("inbox" if row["origin"] in {"inbox", "document"} else
                     "mail" if row["origin"] in {"mail", "contact"} else
                     row["origin"] if row["origin"] in {"drop", "event", "lint", "state"} else "housekeeping")
            blocks[("proposal", row["id"])] = (group, self._proposal_block(row))
        for record in self.ledger.list_undo(limit=UNDO_SHOWN):
            blocks[("undo", record["id"])] = ("undo", self._undo_block(record))
        return blocks

    @staticmethod
    def _prior_blocks(text: str) -> dict[tuple[str, str], str]:
        prior: dict[tuple[str, str], str] = {}
        if REVIEW_BEGIN in text and REVIEW_END in text:
            chunks = ITEM_RE.split(text.split(REVIEW_BEGIN, 1)[1].split(REVIEW_END, 1)[0])
            for index in range(1, len(chunks), 3):
                prior[(chunks[index], chunks[index + 1])] = chunks[index + 2].split(ITEM_END, 1)[0].strip()
        return prior

    @staticmethod
    def _managed(intro: str, sections: list[tuple[str, list[tuple[tuple[str, str], str]]]],
                 prior: dict[tuple[str, str], str], attention: list[str]) -> str:
        out = [REVIEW_BEGIN, "", intro, ""]
        for title, items in sections:
            out += [title, ""]
            if not items:
                out += ["_Nothing waiting._", ""]
            for (kind, entry_id), body in items:
                shown = body
                existing = prior.get((kind, entry_id))
                if existing is not None:
                    try:
                        if _normalized_block(existing) == _normalized_block(body):
                            shown = existing  # keeps a tick the agent has not read yet
                        elif kind == "proposal" and FIELD_BEGIN in body:
                            _, typed = split_feedback(existing)  # keep what you typed in the field
                            shown = with_feedback(body, typed)
                    except ValueError:
                        shown = existing  # don't erase a correction while it is being typed
                out += [f"<!-- gtd-agent:{kind} id={entry_id} -->", shown, ITEM_END, ""]
        return "\n".join([*out, *attention, REVIEW_END])

    def render_reviews(self) -> None:
        """APPROVAL: decisions waiting for you. LOG: applied changes with Undo boxes, above the history."""
        expected = self._expected_blocks()
        path = self.settings.vault_path(self.settings.review)
        current = read_snapshot(path) if path.exists() else None
        text = current.text if current else "# APPROVAL\n\nThe GTD agent's proposals. Rules: [[VAULT_RULES]].\n"
        sections = [(title, [(key, body) for key, (group, body) in expected.items() if group == name])
                    for name, title in (("inbox", "## Inbox"), ("mail", "## Mail"), ("drop", "## Why did you drop these?"),
                                        ("event", "## What came out of it?"),
                                        ("lint", "## Vault choices"), ("state", "## Current state handovers"),
                                        ("housekeeping", "## Housekeeping"))]
        attention: list[str] = []
        applying = self.ledger.list("applying")
        if applying:
            attention += ["## Needs attention", "",
                          "A write was interrupted. Run `reconcile` (it checks whether the change landed).", ""]
            attention += [f"- `{row['id']}` · {_review_text(json.loads(row['proposal_json']).get('summary', ''))}"
                          for row in applying] + [""]
        managed = self._managed(APPROVAL_INTRO, sections, self._prior_blocks(text), attention)
        if REVIEW_BEGIN in text and REVIEW_END in text:
            start = text.index(REVIEW_BEGIN)
            end = text.index(REVIEW_END, start) + len(REVIEW_END)
            new_text = text[:start] + managed + text[end:]
        elif REVIEW_BEGIN not in text and REVIEW_END not in text:
            new_text = text.rstrip("\n") + "\n\n" + managed + "\n"
        else:
            raise RuntimeError("APPROVAL.md has an incomplete managed block; restore both markers")
        if current is None or new_text != current.text:
            atomic_replace(path, new_text, self.settings.state_dir, current.sha256 if current else None,
                           current.newline if current else "\n", backup=False)
        self._render_log(expected)

    def _render_log(self, expected: dict[tuple[str, str], tuple[str, str]]) -> None:
        path = self.settings.vault_path(self.settings.log_note)
        snapshot = read_snapshot(path) if path.exists() else None
        old = snapshot.text if snapshot else ""
        if old and (REVIEW_BEGIN in old) != (REVIEW_END in old):
            raise RuntimeError("LOG.md has an incomplete managed block; restore both markers")
        _, history = _log_parts(old)
        undo = [(key, body) for key, (group, body) in expected.items() if group == "undo"]
        attention: list[str] = []
        stuck = self.ledger.list_undo_attention()
        if stuck:
            attention += ["## Undo needs attention", "",
                          "A later edit blocked a reversal, or a reversal stopped partway. Check the files and agent "
                          "backups.", ""]
            attention += [f"- `{row['id']}` · {row['status']} · {_review_text(row['summary'])}" for row in stuck] + [""]
        managed = self._managed(LOG_INTRO, [("## Undo recent changes", undo)], self._prior_blocks(old), attention)
        self._write_log(snapshot, managed, history)

    def _chunks(self, rel: str) -> list[tuple[str, str, str]]:
        path = self.settings.vault_path(rel)
        if not path.exists():
            return []
        text = read_snapshot(path).text
        if REVIEW_BEGIN not in text or REVIEW_END not in text:
            return []
        chunks = ITEM_RE.split(text.split(REVIEW_BEGIN, 1)[1].split(REVIEW_END, 1)[0])
        return [(chunks[index], chunks[index + 1], chunks[index + 2]) for index in range(1, len(chunks), 3)]

    def sync_review_requests(self) -> list[str]:
        """Apply the boxes you ticked: decisions in APPROVAL, Undo in LOG."""
        chunks = [*self._chunks(self.settings.review), *self._chunks(self.settings.log_note)]
        if not chunks:
            return []
        expected = self._expected_blocks()
        outcomes: list[str] = []
        for kind, entry_id, remainder in chunks:
            if ITEM_END not in remainder:
                continue
            body = remainder.split(ITEM_END, 1)[0].strip()
            choice = _selected_choice(body)
            if choice is None:
                continue
            canonical = expected.get((kind, entry_id))
            if canonical is None:
                outcomes.append(f"{entry_id}: no longer pending")
                continue
            if _normalized_block(body) != _normalized_block(canonical[1]):
                outcomes.append(f"{entry_id}: the preview text was edited; nothing applied")
                continue
            try:
                if kind == "undo":
                    if choice == "Undo":
                        outcomes.append(self.undo(entry_id, render=False))
                    continue
                row = self.ledger.get(entry_id)
                stored = json.loads(row["proposal_json"]) if row is not None else {}
                if stored.get("choices"):
                    outcomes.append(self.answer(entry_id, choice, render=False))
                elif choice == "Approve" or choice.startswith("Approve · show "):
                    outcomes.append(self.approve(entry_id, render=False,
                                                 selected=_selected_items(body) if stored.get("items") else None,
                                                 start_lead=_chosen_lead(choice, stored)))
                elif choice == "Reject":
                    self.reject(entry_id, render=False)
                    outcomes.append(f"Rejected {entry_id}")
                elif choice == "Set aside":
                    self.reject(entry_id, render=False, status="set_aside")
                    outcomes.append(f"Set aside {entry_id}")
                elif choice == "Save reason":
                    _, reason = split_feedback(body)
                    outcomes.append(self.save_drop_reason(entry_id, reason, render=False))
                elif choice == "Save":
                    _, answer = split_feedback(body)
                    outcomes.append(self.save_event_outcome(entry_id, answer, render=False))
                elif choice == "Skip":
                    self.reject(entry_id, render=False, status="skipped")
                    outcomes.append(f"Skipped {entry_id}")
                elif choice == "Teacher feedback":
                    _, feedback = split_feedback(body)
                    if not feedback:
                        outcomes.append(f"{entry_id}: write Your correction before checking Teacher feedback")
                    else:
                        outcome = self.submit_teacher_feedback(entry_id, feedback, canonical[1])
                        if outcome:
                            outcomes.append(outcome)
            except (OSError, ValueError, RuntimeError) as exc:
                outcomes.append(f"{entry_id}: {exc}")
        if outcomes:
            self.render_reviews()
        return outcomes
