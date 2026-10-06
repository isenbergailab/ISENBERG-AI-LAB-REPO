"""Watch edits: project upkeep after the user edits a project, area or list note by hand.

Each scan compares every watched note with the text the agent last read (the ledger keeps it). Ticks, cancels,
dates and status need no model: promotion, drop reasons, TODAY and the vault check already follow them. Three edits
in a project take judgment, so GLM reads them and they come back as proposals:
- a new decision line: the open questions it settles and the steps it creates;
- an open question answered in place (the answer written after it on its line): the decision to record and the
  question to close;
- a changed outcome: a Log line, the steps that no longer serve it and the steps it now needs.
A note is read once it has sat untouched edit_quiet_minutes. Private projects get rules only. The agent's own
writes move the baseline with them (Agent.apply_ops), so they never read as your edits.
"""
from __future__ import annotations

import difflib
import re
from typing import Any

from .clarification import compile_ops, new_marker
from .core import CONTEXTS, SECRET_RE, digest
from .decisions import QUESTION_RE, decisions_note
from .markdown import HEADING_RE, section_bounds
from .providers import ProviderError
from .tasks import TAG_RE, clean_text, normalize
from .vault import Project, Vault
from .workflow import PROPOSAL_VERSION

BULLET_RE = re.compile(r"^[-*+]\s+(?!\[.\])(.+?)\s*$")
REWORDED = 0.6  # a line this similar to one you removed is a rewording, not a new decision
LOG_CHARS = 200


def watched_notes(vault: Vault) -> dict[str, Project | None]:
    """Every project hub and separate decisions note (with its project), area note and list note."""
    notes: dict[str, Project | None] = {}
    for project in vault.projects.values():
        if project.archived:
            continue
        notes[project.rel] = project
        target, separate = decisions_note(vault, project)
        if separate:
            notes[target] = project
    for area in vault.areas.values():
        notes.setdefault(area.rel, None)
    for rel in (vault.settings.single_actions, vault.settings.someday):
        notes.setdefault(rel, None)
    return {rel: project for rel, project in notes.items() if rel in vault.notes}


def _section(text: str, name: str) -> list[str]:
    lines = text.split("\n")
    bounds = section_bounds(lines, name, 2)
    return lines[bounds[0] + 1:bounds[1]] if bounds else []


def _decision_region(text: str, separate: bool) -> list[str]:
    """The lines that hold decisions: the hub's Decisions section, or a decisions note outside its frontmatter,
    Open decisions and Superseded."""
    if not separate:
        return _section(text, "Decisions")
    lines = text.split("\n")
    start = 0
    if lines and lines[0].strip() == "---":
        start = next((n + 1 for n in range(1, len(lines)) if lines[n].strip() == "---"), 0)
    kept, skipping = [], False
    for line in lines[start:]:
        heading = HEADING_RE.match(line)
        if heading and len(heading.group(1)) == 2:
            skipping = heading.group(2).strip().casefold() in {"open decisions", "superseded"}
            continue
        if not skipping:
            kept.append(line)
    return kept


def _decision_body(text: str) -> str:
    """'**Use X** · because Y · 2026-09-28' reads as 'Use X · because Y'."""
    parts = [part.strip() for part in re.split(r"\s+·\s+", text.replace("**", "")) if part.strip()]
    if parts and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[-1]):
        parts = parts[:-1]
    return " · ".join(parts)


def new_decisions(old: str, new: str, separate: bool) -> list[str]:
    """Decision lines you added, as 'decision · why'. A moved or reworded line is not a new decision."""
    before, after = _decision_region(old, separate), _decision_region(new, separate)
    opcodes = difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
    removed = [line for tag, i1, i2, _, _ in opcodes if tag in {"delete", "replace"} for line in before[i1:i2]]
    existing = {line.strip() for line in before}
    found = []
    for tag, _, _, j1, j2 in opcodes:
        if tag not in {"insert", "replace"}:
            continue
        for line in after[j1:j2]:
            match = BULLET_RE.match(line)
            if match is None or line.strip() in existing or any(
                    difflib.SequenceMatcher(None, line, gone).ratio() >= REWORDED for gone in removed):
                continue
            body = _decision_body(match.group(1))
            if body and not SECRET_RE.search(body) and "#private" not in body.casefold():
                found.append(body)
    return found


def answered_questions(old: str, new: str, section: str) -> list[tuple[str, str]]:
    """(the question line as it reads now, question plus answer) for each open question you answered on its own
    line, such as '- Which soil mix? Half compost, it drains well'."""
    before, after = _section(old, section), _section(new, section)
    asked = []
    for line in before:
        match = QUESTION_RE.match(line.rstrip())
        if match and not line.startswith((" ", "\t")):
            asked.append(match.group(2).strip())
    found = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag != "replace":
            continue
        for line in after[j1:j2]:
            match = QUESTION_RE.match(line.rstrip())
            if match is None or line.startswith((" ", "\t")):
                continue
            text = match.group(2).strip()
            question = next((q for q in asked if text.startswith(q) and len(text) > len(q)), None)
            if question and text[len(question):].strip(" -–—:→>") and not SECRET_RE.search(text):
                found.append((line.rstrip(), text))
    return found


def outcome_change(old: str, new: str) -> tuple[str, str] | None:
    before = " ".join(line.strip() for line in _section(old, "Outcome") if line.strip())
    after = " ".join(line.strip() for line in _section(new, "Outcome") if line.strip())
    if not after or normalize(before) == normalize(after) or SECRET_RE.search(after):
        return None
    return before, after


class Watcher:
    def __init__(self, keeper: Any) -> None:
        self.keeper = keeper
        self.agent = keeper.agent
        self.ledger = keeper.ledger
        self.settings = keeper.settings

    def run(self, vault: Vault) -> list[str]:
        made = 0
        for rel, project in watched_notes(vault).items():
            text = vault.notes[rel].text
            before = self.ledger.watched(rel)
            if before is None:
                self.ledger.set_watched(rel, text)  # first sight: nothing to compare yet
                continue
            if before == text or not self.keeper._quiet(vault, rel):
                continue
            if project is not None and not vault.is_private_project(project) and self.settings.mode == "approval":
                made += self.upkeep(vault, project, rel, before, text)
            self.ledger.set_watched(rel, text)
        return [f"Read {made} edit{'s' if made != 1 else ''} into proposals"] if made else []

    def _propose(self, key: str, build: Any) -> int:
        if self.ledger.get_by_key(key) is not None:
            return 0
        proposal = build()
        proposal["engine_version"] = PROPOSAL_VERSION
        self.ledger.upsert("watch", key, proposal)
        return 1

    def upkeep(self, vault: Vault, project: Project, rel: str, before: str, after: str) -> int:
        today = self.agent.today()
        target, separate = decisions_note(vault, project)
        note = rel.rsplit("/", 1)[-1][:-3]
        made = 0
        if rel == target:
            for body in new_decisions(before, after, separate):
                made += self._propose(f"watchdecision|{rel}|{digest(body)[:16]}", lambda body=body: dict(
                    self.agent.decision_proposal(vault, project, body, {"source": f"You wrote in {note}: {body}"},
                                                 today, False, record=False), watched=rel))
        section = "Open decisions" if separate and rel == target else "Open questions" if rel == project.rel else ""
        if section:
            for line, body in answered_questions(before, after, section):
                made += self._propose(f"watchanswer|{rel}|{digest(line)[:16]}", lambda line=line, body=body: dict(
                    self.agent.decision_proposal(vault, project, body, {"source": f"You answered in {note}: {body}"},
                                                 today, False, settles=(line,)), watched=rel))
        if rel == project.rel and (change := outcome_change(before, after)):
            made += self._propose(f"watchoutcome|{project.key}|{digest(change[1])[:16]}",
                                  lambda: self.outcome_proposal(vault, project, *change))
        return made

    def outcome_proposal(self, vault: Vault, project: Project, old: str, new: str) -> dict[str, Any]:
        """A Log line for the new outcome, plus the steps GLM thinks no longer fit (unticked) and the ones it now
        needs (ticked)."""
        today = self.agent.today()
        steps = [t for t in project.tasks if t.is_open and not t.is_waiting and t.section.casefold() == "steps"
                 and t.heading.casefold() != "done" and not t.indent]
        listed = [{"id": f"s{number}", "text": step.title} for number, step in enumerate(steps, 1)]
        note, answer = "", None
        if not self.settings.remote_inference or not self.agent._provider_ready():
            note = "Remote inference is off, so the steps were not checked."
        else:
            try:
                answer = self.agent.provider.outcome_check(project.key, old, new, listed, today)
            except (ProviderError, ValueError, RuntimeError) as exc:
                note = f"GLM could not check the steps ({exc})."
        drops, extra, explanation = [], [], ""
        if isinstance(answer, dict):
            ids = {item["id"]: step for item, step in zip(listed, steps)}
            drops = [ids[i] for i in dict.fromkeys(answer.get("drop_step_ids") or []) if i in ids]
            explanation = re.sub(r"\s+", " ", str(answer.get("explanation") or ""))[:300]
            for step in (answer.get("new_steps") or [])[:3]:
                try:
                    title = clean_text(str(step.get("title") or ""), 150) if isinstance(step, dict) else ""
                except ValueError:
                    continue
                if title and "[[" not in title and not TAG_RE.search(title):
                    extra.append((title, step.get("context") if step.get("context") in CONTEXTS else "#computer"))
        shown = new if len(new) <= LOG_CHARS else new[:LOG_CHARS - 1].rstrip() + "…"
        base_ops = [{"op": "append_log", "path": project.rel, "entry": f"{today.isoformat()} Outcome changed to: {shown}"}]
        items: list[dict[str, Any]] = [
            {"label": f"Drop step · {step.title}", "checked": False,
             "ops": [{"op": "cancel_task", "path": step.path, "match": step.raw, "done": today.isoformat()}]}
            for step in drops]
        gave_next = False
        for title, context in extra:
            draft = {"route": "next_action", "title": title, "context": context, "project": project, "dates": {},
                     "extra_tags": [], "force_queue": gave_next}
            try:
                ops, summary, title = compile_ops(vault, draft, capture=new, reference=today, marker=new_marker())
            except (ValueError, RuntimeError):
                continue
            gave_next = gave_next or any(op.get("position") == "steps" for op in ops)
            items.append({"label": f"Step · {title} → {summary}", "ops": ops, "checked": True})
        reason = " ".join(part for part in [
            f"You changed the outcome of {project.key}. Approve to log it; tick any step to drop (the agent does not "
            "ask why, the new outcome is the reason).", explanation, note] if part)
        return {"kind": "outcome_change", "title": f"{project.key}: {shown}", "summary": f"Log in {project.key}",
                "reason": reason, "source": f"Outcome was: {old or '(empty)'}", "base_ops": base_ops,
                "items": items, "watched": project.rel,
                "ops": [*base_ops, *(op for item in items if item["checked"] for op in item["ops"])]}
