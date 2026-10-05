"""Current state upkeep: the agent keeps each project's `## Current state` true as bookkeeping.

The section says where the project stands: next step, next event, waits, blockers, open questions, due date and the
last change (the newest Log line). An event belongs to a project when its title or notes name the project's key or
an alias. It holds dates, never day counts, so it changes only when the project does.
A section that holds the placeholder (or nothing) belongs to the agent. Text from before this feature is replaced
once through APPROVAL: approve and the agent keeps it from then on; reject and it stays yours. Edit an agent-kept
section and it becomes yours; write the placeholder back to hand it back. A note without the section is left alone.
"""
from __future__ import annotations

import re
from datetime import datetime

from .calendar_ics import Event
from .clarification import NOT_WRITTEN
from .decisions import decisions_note, open_questions
from .markdown import section_bounds
from .vault import Project, Vault

SECTION = "Current state"
PLACEHOLDER = NOT_WRITTEN[SECTION]
SHORT = 80
LISTED = 3  # items per line before "(+N more)"
NEXT_EVENT = "- Next event: "


def section_body(text: str, name: str = SECTION) -> str | None:
    """The section's text without its heading, trimmed; None when the note has no such section."""
    lines = text.split("\n")
    bounds = section_bounds(lines, name, 2)
    if bounds is None:
        return None
    return "\n".join(line.rstrip() for line in lines[bounds[0] + 1:bounds[1]]).strip()


def _short(text: str) -> str:
    return text if len(text) <= SHORT else text[:SHORT - 1].rstrip() + "…"


def _joined(items: list[str]) -> str:
    more = f" (+{len(items) - LISTED} more)" if len(items) > LISTED else ""
    return " · ".join(items[:LISTED]) + more


def events_by_project(vault: Vault, events: list[Event]) -> dict[str, list[Event]]:
    """The events that name each active project: its key or an alias (3 characters or more), in the title or the
    notes, as whole words."""
    found: dict[str, list[Event]] = {}
    for project in vault.projects.values():
        if project.archived or project.status != "active":
            continue
        names = {name.strip() for name in (project.key, project.key.replace("_", " "), *project.aliases)
                 if len(name.strip()) >= 3}
        pattern = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
                             + r")(?![\w-])", re.IGNORECASE)
        hits = [event for event in events if pattern.search(event.summary) or pattern.search(event.notes)]
        if hits:
            found[project.key] = sorted(hits, key=lambda event: event.start)
    return found


def event_line(event: Event | None) -> str | None:
    if event is None:
        return None
    when = event.start.date().isoformat() if event.all_day else f"{event.start:%Y-%m-%d %H:%M}"
    return f"{NEXT_EVENT}{when} {_short(event.summary)}"


def next_events(vault: Vault, events: list[Event], now: datetime) -> dict[str, str]:
    """Each active project's next event (or the one under way), as its Current state line."""
    lines = {}
    for key, found in events_by_project(vault, events).items():
        line = event_line(next((event for event in found if event.end > now), None))
        if line:
            lines[key] = line
    return lines


def state_text(vault: Vault, project: Project, next_event: str | None = None) -> str:
    lines = []
    if project.status != "active":
        review = f" (review {project.review.isoformat()})" if project.status == "someday" and project.review else ""
        lines.append(f"- Status: {project.status}{review}")
    steps = vault.open_next(project)
    if steps or project.status == "active":
        lines.append("- Next: " + (_joined([_short(t.title) for t in steps]) if steps else "no #next step"))
    if next_event:
        lines.append(next_event)
    waits = [t for t in project.tasks if t.is_open and t.is_waiting]
    if waits:
        lines.append("- Waiting on: " + _joined(
            [_short(t.title) + (f" (follow up {t.due.isoformat()})" if t.due else "") for t in waits]))
    ids = {t.task_id: t for t in project.tasks if t.task_id and t.is_open}
    blocked = [f"{_short(t.title)} waits on {_short(ids[d].title)}"
               for t in project.tasks if t.is_open for d in t.depends_on if d in ids]
    if blocked:
        lines.append("- Blocked: " + _joined(blocked))
    target, separate = decisions_note(vault, project)
    questions = open_questions(vault, project, target, separate)
    if questions:
        lines.append(f"- Open questions: {len(questions)}")
    if project.due:
        lines.append(f"- Due: {project.due.isoformat()}")
    log = vault.notes[project.rel].text.split("\n") if project.rel in vault.notes else []
    bounds = section_bounds(log, "Log", 2)
    newest = next((line[2:].strip() for line in log[bounds[0] + 1:bounds[1]] if line.startswith("- ")), "") \
        if bounds else ""
    if newest:
        lines.append(f"- Last change: {_short(newest)}")
    return "\n".join(lines)
