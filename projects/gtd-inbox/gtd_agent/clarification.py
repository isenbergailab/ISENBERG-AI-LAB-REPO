"""Turn one Inbox capture into a reviewed proposal: model or explicit syntax in, write ops out."""
from __future__ import annotations

import calendar
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from .core import CONTEXTS, SECRET_RE
from .dates import resolve_date_evidence, resolve_end_evidence, resolve_time_evidence
from .markdown import iter_links
from .tasks import DATE_FIELDS, TAG_RE, clean_text, format_task, normalize, plain_words
from .vault import Project, Vault, project_key

ROUTES = ("next_action", "waiting_for", "project_note", "new_project", "someday_maybe",
          "completion_report", "calendar_event", "needs_clarification")
TIME_ESTIMATES = ("", "5m", "15m", "30m", "1h", "2h", "4h")
TEXT_FIELDS = ("title", "project_key", "area_key", "person", "what", "due_evidence", "start_evidence",
               "since_evidence", "completion_evidence", "name_excerpt", "first_step", "action_id", "recurrence",
               "time_evidence", "end_evidence", "explanation", "lesson", "assumptions", "question")
LATER_FIELDS = ("recurrence", "time_evidence", "end_evidence")  # absent from interpretations saved before them
ENUM_FIELDS = {"route": ROUTES, "context": CONTEXTS, "project_status": ("active", "someday"),
               "time_estimate": TIME_ESTIMATES}
FIELDS: dict[str, dict[str, Any]] = {name: {"type": "string"} for name in TEXT_FIELDS}
FIELDS.update({name: {"type": "string", "enum": list(values)} for name, values in ENUM_FIELDS.items()})
SCHEMA = {"type": "object", "additionalProperties": False, "properties": FIELDS, "required": list(FIELDS)}
EVIDENCE_FIELDS = ("person", "due_evidence", "start_evidence", "since_evidence", "completion_evidence",
                   "name_excerpt", "first_step", "time_evidence", "end_evidence")
CONTEXT_HEADING = {"#computer": "@computer", "#calls": "@calls", "#anywhere": "@anywhere", "#errands": "@errands"}
SPECIAL_TAGS = {"#next", "#waiting", "#someday", "#private", "#research", "#event"}
EVENT_TIMES_RE = re.compile(r"(?i)(?<![\d:])(\d{1,2}:\d{2}(?:\s*[ap]\.?m\.?)?|\d{1,2}\s*[ap]\.?m\.?)(?![\w:])"
                            r"(?:\s*[-–]\s*(\d{1,2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?)(?![\w:]))?")
EVENT_NEEDS = "An #event needs a day and a time, like 'Dentist #event 2026-10-14 15:30' (an end is optional: 15:30-16:15)"
REPEAT_CUE_RE = re.compile(r"(?i)\b(?:every|each|daily|weekly|biweekly|fortnightly|monthly|yearly|annually)\b|🔁")
REPEAT_IN_CAPTURE_RE = re.compile(r"🔁️?\s*(every[^📅🛫⏳➕✅❌#\[\]]*)")
_DAY_NAMES = {name.casefold(): name for name in calendar.day_name}
_MONTH_NAMES = {name.casefold(): name for name in calendar.month_name if name}
_REPEAT_WORDS = {"day", "days", "weekday", "weekdays", "week", "weeks", "month", "months", "year", "years",
                 *_DAY_NAMES, *_MONTH_NAMES}


def clean_recurrence(rule: str) -> str:
    """A Tasks repeat rule such as 'every week on Monday'. Day and month names keep their capitals."""
    rule = re.sub(r"\s+", " ", rule.strip()).casefold()
    if not re.fullmatch(r"every [a-z0-9 ,]{2,60}", rule) or not set(re.findall(r"[a-z]+", rule)) & _REPEAT_WORDS:
        raise ValueError("A repeat must look like 'every week on Monday' or 'every month on the 15th'")
    names = {**_DAY_NAMES, **_MONTH_NAMES}
    return re.sub(r"[a-z]+", lambda m: names.get(m.group(), m.group()), rule)


def lead_days(rule: str) -> int:
    """How long before each due date a repeating task should appear (its 🛫 start)."""
    words = set(re.findall(r"[a-z]+", rule.casefold()))
    if words & {"day", "days", "weekday", "weekdays"}:
        return 0
    if words & {"year", "years", *_MONTH_NAMES}:
        return 14
    if words & {"month", "months"}:
        return 5
    return 2


def lead_options(rule: str) -> list[int]:
    """Starts to offer for a repeating duty, in days before each due date. Daily rhythms get none."""
    words = set(re.findall(r"[a-z]+", rule.casefold()))
    if words & {"day", "days", "weekday", "weekdays"}:
        return []
    if words & {"year", "years", *_MONTH_NAMES}:
        options = [0, 7, 14, 30]
    elif words & {"month", "months"}:
        options = [0, 2, 5, 14]
    elif re.search(r"every (?:[2-9]|\d{2,}) weeks", rule.casefold()):
        options = [0, 1, 2, 7]
    else:
        options = [0, 1, 2]
    return sorted({*options, lead_days(rule)})


def lead_label(days: int) -> str:
    return {0: "on the due day", 1: "1 day before", 7: "a week before", 14: "2 weeks before",
            30: "a month before"}.get(days, f"{days} days before")


def start_control(days: int, default: int) -> str:
    """The Approve line that also picks when each copy starts showing."""
    return f"Approve · show {lead_label(days)}" + (" (as shown)" if days == default else "")


def first_due(rule: str, reference: date) -> date:
    """First due date for a repeat with no stated date: the next matching day, else the capture date."""
    words = set(re.findall(r"[a-z]+", rule.casefold()))
    days = [i for i, name in enumerate(calendar.day_name) if name.casefold() in words]
    months = [i for i, name in enumerate(calendar.month_name) if name and name.casefold() in words]
    nth = re.search(r"on the (\d{1,2})(?:st|nd|rd|th)\b", rule.casefold())
    if days and not months:
        return min(reference + timedelta(days=(day - reference.weekday()) % 7) for day in days)
    if nth:
        day_of_month = int(nth.group(1))
        candidates = []
        for month in (months or range(1, 13)):
            for year in (reference.year, reference.year + 1):
                last = calendar.monthrange(year, month)[1]
                candidate = date(year, month, min(day_of_month, last))
                if candidate >= reference:
                    candidates.append(candidate)
        if candidates:
            return min(candidates)
    return reference
ROUTE_LABEL = {"next_action": "Next action", "waiting_for": "Waiting for", "project_note": "Project log",
               "new_project": "New project", "someday_maybe": "Someday / maybe", "completion_report": "Completion",
               "follow_up": "Follow-up", "lint_fix": "Vault fix", "promotion": "Promote next step",
               "someday_topic": "Someday topic", "research_report": "Research report",
               "document": "Inbox file", "decision": "Decision", "calendar_event": "Calendar event",
               "drop_reason": "Why drop", "drop_knock": "After a drop", "lint_choice": "Vault choice",
               "outcome_change": "Outcome changed", "current_state": "Current state",
               "manual": "Needs clarification", "mail": "Mail", "contact": "Contact",
               "after_event": "What came out of it?", "after_event_steps": "Steps after an event"}


def blank_draft(**values: str) -> dict[str, str]:
    draft = {name: "" for name in FIELDS}
    draft.update(route="next_action", context="#anywhere", project_status="active", time_estimate="")
    draft.update(values)
    return draft


def validate_interpretation(value: Any, evidence: str, projects: list[dict], areas: list[dict],
                            items: list[dict], *, capture_checked: bool = False) -> dict[str, str]:
    if isinstance(value, dict) and any(name not in value for name in LATER_FIELDS):
        value = {**{name: "" for name in LATER_FIELDS}, **value}  # interpretations saved before these fields
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError("Wrong interpretation fields")
    for name in FIELDS:
        text = value[name]
        if not isinstance(text, str) or len(text) > 1000 or any(c in text for c in "\r\n\x00"):
            raise ValueError(f"Invalid interpretation field: {name}")
        if name in ENUM_FIELDS and text not in ENUM_FIELDS[name]:
            raise ValueError(f"Invalid interpretation choice: {name}")
        if SECRET_RE.search(text) or "<!--" in text or "-->" in text:
            raise ValueError("Unsafe interpretation content")
    if value["project_key"] and value["project_key"] not in {p["key"] for p in projects}:
        raise ValueError("Project must be one of the supplied projects")
    if value["area_key"] and value["area_key"] not in {a["key"] for a in areas}:
        raise ValueError("Area must be one of the supplied areas")
    if value["action_id"] and value["action_id"] not in {i["id"] for i in items}:
        raise ValueError("Completion must select a supplied open item")
    if value["route"] == "waiting_for" and value["action_id"]:
        chosen = next(i for i in items if i["id"] == value["action_id"])
        if chosen.get("waiting"):  # a wait replaces an open action, never another wait: file it and tick nothing
            value["action_id"] = ""
            value["assumptions"] = (f"{value['assumptions']} Filed as a new wait; your wait '{chosen['title']}' "
                                    "stays as it is.").strip()[:1000]
    folded = evidence.casefold()
    for name in EVIDENCE_FIELDS:
        if value[name] and value[name].casefold() not in folded:
            raise ValueError(f"{name} must be copied from the capture")
    route = value["route"]
    if route == "waiting_for":
        person = value["person"].strip()
        if not person or not value["what"].strip():
            raise ValueError("Waiting For needs a responder and an expected result")
        if not any(c.isalpha() for c in person) or re.match(r"^\d{1,4}[-/]\d", person):
            raise ValueError("A date cannot be the Waiting For responder")
    if value["recurrence"]:
        if route != "next_action":
            raise ValueError("Only a next action can repeat")
        if not REPEAT_CUE_RE.search(evidence):
            raise ValueError("recurrence needs repeat words such as 'every' in the capture")
        value["recurrence"] = clean_recurrence(value["recurrence"])
    if route == "completion_report" and (not value["action_id"] or not (capture_checked or value["completion_evidence"])):
        raise ValueError("Completion needs a matching open item and explicit completion evidence")
    if route == "project_note" and not value["project_key"]:
        raise ValueError("A project note needs a matching project")
    if route == "calendar_event" and not (value["due_evidence"] and value["time_evidence"]):
        raise ValueError(EVENT_NEEDS)
    if route == "needs_clarification":
        if not value["question"]:
            raise ValueError("An unresolved capture needs a specific question")
    elif not value["explanation"] or not value["lesson"]:
        raise ValueError("A resolved capture needs an explanation and a lesson")
    return value


# ---------------------------------------------------------------- context for the model
def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold())) - {
        "i", "a", "the", "to", "and", "for", "of", "with", "in", "my", "it", "is", "on", "at", "me"}


def project_candidates(vault: Vault, capture: str, limit: int = 40) -> list[dict[str, Any]]:
    words = _words(capture)
    rows = []
    for project in vault.projects.values():
        if project.archived or vault.is_private_project(project) or project.status in {"done", "dropped"}:
            continue
        name = project.key.replace("_", " ").title()
        terms = _words(" ".join([project.key.replace("_", " "), *project.aliases]))
        rows.append((len(words & terms), {"key": project.key, "name": name, "aliases": project.aliases[:6],
                                          "status": project.status, "area": project.area or ""}))
    rows.sort(key=lambda row: (-row[0], row[1]["key"]))
    return [row for _, row in rows[:limit]]


def area_candidates(vault: Vault) -> list[dict[str, Any]]:
    return [{"key": area.key, "aliases": area.aliases[:6]} for area in sorted(vault.areas.values(), key=lambda a: a.key)]


def item_candidates(vault: Vault, capture: str, limit: int = 8) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    words = _words(capture)
    ranked = []
    for task in vault.tasks:
        if not task.is_open or vault.is_private_task(task) or SECRET_RE.search(task.raw):
            continue
        project = vault.project_for_path(task.path)
        if project is None and task.path != vault.settings.single_actions and vault.area_for_path(task.path) is None:
            continue
        score = len(words & _words(task.title))
        if project is not None and (project.key.casefold() in capture.casefold()
                                    or any(a.casefold() in capture.casefold() for a in project.aliases)):
            score += 3
        if score:
            ranked.append((score, task, project))
    ranked.sort(key=lambda row: (-row[0], row[1].path, row[1].line_no))
    items, lookup = [], {}
    for _, task, project in ranked[:limit]:
        items.append({"id": task.ref, "title": task.title, "project": project.key if project else "",
                      "waiting": task.is_waiting})
        lookup[task.ref] = task
    return items, lookup


# ---------------------------------------------------------------- explicit syntax (no model)
EMOJI_DATE_RE = re.compile(r"(" + "|".join(sorted((re.escape(k) for k in DATE_FIELDS), key=len, reverse=True))
                           + r")️?\s*(\d{4}-\d{2}-\d{2})")


def parse_structured(text: str, vault: Vault) -> dict[str, Any] | None:
    """Captures written in vault syntax are filed without any model call.

    Raises ValueError for vault syntax that is present but wrong (a bad repeat rule), so you see why.
    """
    tags = [tag.casefold() for tag in TAG_RE.findall(text)]
    if "#research" in tags:
        return None  # research requests go through the interpreter so the project and review date are read
    recurrence = None
    repeat = REPEAT_IN_CAPTURE_RE.search(text)
    if "🔁" in text and not repeat:
        raise ValueError("A repeat must look like '🔁 every week on Monday' or '🔁 every month on the 15th'")
    if repeat:
        recurrence = clean_recurrence(repeat.group(1))
        text = text[:repeat.start()] + " " + text[repeat.end():]
    contexts = [tag for tag in tags if tag in CONTEXTS]
    targets = [link.target for link in iter_links(text)]
    dates: dict[str, date] = {}
    for emoji, value in EMOJI_DATE_RE.findall(text):
        name = DATE_FIELDS.get(emoji)
        if name:
            dates[name] = date.fromisoformat(value)
    project = next((vault.projects[t] for t in targets if t in vault.projects), None)
    area = next((t for t in targets if t in vault.areas), None)
    unknown = [t for t in targets if t not in vault.projects and t not in vault.areas]
    body = EMOJI_DATE_RE.sub("", text)
    body = re.sub(r"\[\[[^\]]+\]\]", "", body)
    body = TAG_RE.sub("", body)
    body = re.sub(r"\s+", " ", body).strip(" -")
    extra = [tag for tag in tags if tag not in CONTEXTS and tag not in SPECIAL_TAGS]
    if "#private" in tags:
        extra.append("#private")
    base = {"project": project, "area": area, "dates": dates, "extra_tags": extra, "unknown_links": unknown,
            "recurrence": recurrence, "explicit_next": "#next" in tags}
    if "#event" in tags:
        days = re.findall(r"\d{4}-\d{2}-\d{2}", text)
        rest = re.sub(r"(?:📅️?\s*)?\d{4}-\d{2}-\d{2}", " ", body)
        times = EVENT_TIMES_RE.search(rest)
        if len(days) != 1 or times is None:
            raise ValueError(EVENT_NEEDS)
        start = resolve_time_evidence(times[1])
        title = re.sub(r"\s+", " ", rest[:times.start()] + " " + rest[times.end():]).strip(" -·,")
        if not title:
            raise ValueError("An #event needs a few words saying what it is")
        return dict(base, route="calendar_event", title=title, event_day=date.fromisoformat(days[0]),
                    start_time=start, end_evidence=times[2] or "")
    if "#waiting" in tags:
        person, sep, what = body.partition(":")
        if not sep or not person.strip() or not what.strip():
            return None
        return dict(base, route="waiting_for", person=person.strip(), what=what.strip(), title=body)
    if "#someday" in tags and body:
        review = dates.get("start") or dates.get("scheduled") or dates.get("due")
        return dict(base, route="someday_maybe", title=body, dates={"review": review})
    if len(contexts) == 1 and body:
        return dict(base, route="next_action", context=contexts[0], title=body)
    if recurrence:
        raise ValueError("A repeating task needs one context tag, such as #computer")
    return None


# ---------------------------------------------------------------- compiling ops
REFERENCE_BLOCK = ("### Reference", "```dataview",
                   'LIST FROM "04_REFERENCE" WHERE contains(project, this.file.link) SORT file.name ASC', "```")


NO_STEPS = "_No steps yet. Add the first one with a context tag and `#next`._"
NOT_WRITTEN = {"Why": "_Why it matters to you. Not written yet._",
               "Context": "_Constraints, people, resources: what an outsider needs to know. Not written yet._",
               "Current state": "_The GTD agent keeps this current._"}


def project_note_text(key: str, status: str, area: str | None, due: date | None, outcome: str, today: date, *,
                      review: date | None = None, steps: list[str] | None = None, questions: list[str] | None = None,
                      notes: list[str] | None = None, log: str = "Project created from the Inbox.") -> str:
    """A new project note in the VAULT_RULES template: every section, in order, so it reads cold."""
    lines = ["---", f"key: {key}", "type: project", f"status: {status}",
             f"area: \"[[{area}]]\"" if area else "area:", f"due: {due.isoformat() if due else ''}".rstrip(),
             "priority:", "aliases: []", f"review: {review.isoformat() if review else ''}".rstrip(), "private: false",
             "---", f"# {key}", "",
             "## Outcome", outcome, "",
             "## Why", NOT_WRITTEN["Why"], "",
             "## Context", NOT_WRITTEN["Context"], "",
             "## Current state", NOT_WRITTEN["Current state"], "",
             "## Steps", *(steps or [NO_STEPS]), "",
             "## Deadlines", "",
             "## Waiting For", "",
             "## Decisions", "",
             "## Open questions", *(questions or []), "",
             "## Notes", *(notes or []), "",
             "## Log", f"- {today.isoformat()} {log}", "",
             "## Links", *REFERENCE_BLOCK, ""]
    return "\n".join(lines)


def _project_note_text(key: str, status: str, area: str | None, due: date | None, outcome: str,
                       first_step_line: str | None, capture: str, today: date, review: date | None = None,
                       review_question: str = "") -> str:
    questions = ["> [!question] Review date", f"> {review_question}"] if review_question else None
    return project_note_text(key, status, area, due, outcome, today, review=review,
                             steps=[first_step_line] if first_step_line else None, questions=questions,
                             notes=["### Original Inbox capture", f"> {capture}"])


def research_request_text(key: str, capture: str, today: date) -> str:
    return "\n".join([
        "---", "type: research_request", f"project: \"[[{key}]]\"", f"requested: {today.isoformat()}", "status: open",
        "---", f"# Research request: {key}", "", "Brief, in your words:", f"> {capture}", "",
        "Deliver one Markdown report. Put `request: " + key + "` in its frontmatter. Include a summary, the key "
        "resources with links, a suggested plan with first steps, and sources. Save it in the research reports "
        "folder; the GTD agent proposes filing it.", ""])


def _research_op(vault: Vault, key: str, capture: str, reference: date) -> dict[str, Any]:
    path = f"{vault.settings.agent_dir}/RESEARCH_REQUESTS/{key}.md"
    if vault.settings.vault_path(path).exists():
        raise ValueError(f"A research request for {key} already exists")
    return {"op": "create_file", "path": path, "text": research_request_text(key, capture, reference)}


def _closable(task: Any) -> Any:
    """The agent ticks a task only when the tick needs no plugin: a repeating task must get its next copy."""
    if task.recurrence:
        raise ValueError(f"'{task.title}' repeats. Tick it in Obsidian so the Tasks plugin creates the next one")
    return task


def _shown(task: Any) -> str:
    title = task.title
    for link in task.links:
        title = title.replace(link, "")
    return re.sub(r"\s+", " ", title).strip()


def compile_ops(vault: Vault, draft: dict[str, Any], *, capture: str, reference: date, marker: str,
                matched_task: Any = None) -> tuple[list[dict[str, Any]], str, str]:
    """Return (ops, summary, title) for a validated interpretation or explicit capture."""
    settings = vault.settings
    route = draft["route"]
    project: Project | None = draft.get("project")
    area = draft.get("area")
    dates = draft.get("dates", {})
    block = f"gtd-{marker}"
    if route == "next_action":
        recurrence = draft.get("recurrence")
        title = clean_text(plain_words(draft["title"]))
        tags = [draft["context"], *draft.get("extra_tags", [])]
        if draft.get("time_estimate"):
            tags.append(f"#{draft['time_estimate']}")
        due, start = dates.get("due"), dates.get("start")
        if recurrence and not due:
            due = first_due(recurrence, reference)
        choice = None  # a repeating duty with no stated start asks when each copy should show
        if recurrence and not start and lead_days(recurrence):
            start = due - timedelta(days=lead_days(recurrence))
            choice = {"due": due.isoformat(), "default": lead_days(recurrence), "options": lead_options(recurrence)}

        def task_op(**fields: Any) -> dict[str, Any]:
            return {"op": "add_task", **fields, **({"start_choice": choice} if choice else {})}
        task_dates = {"start": start, "due": due, "created": reference}
        if project is not None:
            if project.status in {"done", "dropped"}:
                raise ValueError(f"{project.key} is {project.status}; set its status back to active first")
            parked = project.status == "someday"
            wants_next = not parked and not draft.get("force_queue") and (
                bool(draft.get("explicit_next")) or bool(recurrence)
                or (project.status == "active" and not vault.open_next(project)))
            if wants_next:
                tags.insert(1, "#next")
            line = format_task(title, tags, dates=task_dates, recurrence=recurrence, block_id=block)
            op = task_op(path=project.rel, section="Steps", position="steps" if wants_next else "queue", line=line,
                         marker=f"^{block}")
            if wants_next:
                summary = f"Next action in {project.key} › Steps"
            elif parked:
                summary = f"Step in {project.key} › Steps (project is someday, so no #next)"
            else:
                summary = f"Step queued in {project.key} › Steps (gets #next when its turn comes)"
            return [op], summary, title
        if recurrence and area and area in vault.areas:
            tags.insert(1, "#next")
            line = format_task(title, tags, dates=task_dates, recurrence=recurrence, block_id=block)
            return ([task_op(path=vault.areas[area].rel, section="Recurring", position="end", line=line,
                             marker=f"^{block}")], f"Recurring duty in {area} › Recurring", title)
        text = f"{title} [[{area}]]" if area else title
        line = format_task(text, tags, dates=task_dates, recurrence=recurrence, block_id=block)
        heading = "Recurring" if recurrence else CONTEXT_HEADING[draft["context"]]
        return ([task_op(path=settings.single_actions, section=heading, position="end", line=line,
                         marker=f"^{block}")], f"Single action › {heading}", title)
    if route == "waiting_for":
        person = clean_text(plain_words(draft["person"]), 120)
        what = clean_text(plain_words(draft["what"]))
        since = dates.get("created") or reference
        follow = dates.get("due") or reference + timedelta(days=7)
        if follow < since:
            raise ValueError("The follow-up date comes before the waiting-since date")
        closes = _closable(matched_task) if matched_task is not None else None
        if closes is not None:  # the ask carried out this action: the wait takes its place
            home = vault.project_for_path(closes.path)
            if project is not None and home is not None and home.key != project.key:
                raise ValueError(f"The action it replaces is in {home.key}, not {project.key}")
            project = project or home
            owner = vault.area_for_path(closes.path)
            area = area or (owner.key if owner else None) or next(
                (link for link in closes.links if link in vault.areas), None)
        text = f"#waiting {person}: {what}"
        if project is None and area:
            text += f" [[{area}]]"
        line = format_task(text, draft.get("extra_tags", []), dates={"created": since, "due": follow}, block_id=block)
        path = project.rel if project is not None else settings.single_actions
        where = f"{project.key} › Waiting For" if project is not None else "Single actions › Waiting For"
        ops = [{"op": "add_task", "path": path, "section": "Waiting For", "position": "end", "line": line,
                "marker": f"^{block}"}]
        if closes is not None:
            ops.append({"op": "complete_task", "path": closes.path, "match": closes.raw, "done": reference.isoformat()})
            where += f"; closes '{_shown(closes)}'"
        return ops, where, f"{person}: {what}"
    if route == "project_note":
        if project is None:
            raise ValueError("A project note needs a project")
        title = clean_text(draft["title"])
        ops = [{"op": "append_log", "path": project.rel, "entry": f"{reference.isoformat()} {title}"}]
        summary = f"Log entry in {project.key}"
        if draft.get("research"):
            ops.append(_research_op(vault, project.key, capture, reference))
            summary += " + research request"
        return ops, summary, title
    if route == "new_project":
        name = draft.get("name") or draft["title"]
        key = project_key(name)
        if key.casefold() in vault.by_stem or any(p.key == key for p in vault.projects.values()):
            raise ValueError(f"A note named {key} already exists")
        status = draft.get("project_status") or "active"
        step_line = None
        if draft.get("first_step"):
            tags = [draft.get("context") or "#anywhere"]
            if status == "active":
                tags.append("#next")
            if draft.get("time_estimate"):
                tags.append(f"#{draft['time_estimate']}")
            step_line = format_task(clean_text(plain_words(draft["first_step"])), tags, dates={"created": reference})
        outcome = clean_text(plain_words(draft["title"]))
        review = dates.get("review") if status == "someday" else None
        text = _project_note_text(key, status, area, dates.get("due"), outcome, step_line, capture, reference,
                                  review=review, review_question=draft.get("review_question", ""))
        path = f"{settings.projects_dir}/{key}/{key}.md"
        ops = [{"op": "create_file", "path": path, "text": text}]
        summary = f"New project {key} ({status}" + (f", review {review.isoformat()})" if review else ")")
        if draft.get("research"):
            ops.append(_research_op(vault, key, capture, reference))
            summary += " + research request"
        return ops, summary, outcome
    if route == "someday_maybe":
        title = clean_text(draft["title"])
        review = dates.get("review")
        text = title + (f" · [[{project.key}]]" if project is not None else "") + \
            (f" · review {review.isoformat()}" if review else "")
        if draft.get("research"):
            raise ValueError("#research needs a project. Describe it as a project, or name an existing one")
        return ([{"op": "append_bullet", "path": settings.someday, "section": "Added from the Inbox", "text": text}],
                "Someday / maybe list" + (f", review {review.isoformat()}" if review else ""), title)
    if route == "calendar_event":
        title = clean_text(plain_words(draft["title"]), 200)
        if draft.get("place"):
            title = f"{title} ({clean_text(plain_words(draft['place']), 120)})"
        if draft.get("event_day") is None or draft.get("start_time") is None:
            raise ValueError(EVENT_NEEDS)
        if draft["event_day"] < reference:
            raise ValueError("That day has passed")
        start = datetime.combine(draft["event_day"], draft["start_time"])
        end = draft.get("end") or resolve_end_evidence(draft.get("end_evidence") or "", start) \
            or start + timedelta(hours=1)
        when = f"{start:%a %b} {start.day} {start:%H:%M}–{end:%H:%M}" if end.date() == start.date() else \
            f"{start:%a %b} {start.day} {start:%H:%M} to {end:%a %b} {end.day} {end:%H:%M}"
        when += ", repeats" if draft.get("repeats") else ""
        if not (settings.calendar_enabled and settings.calendar_source == "folder"):
            return calendar_by_hand(vault, title, when, reference=reference, marker=marker,
                                    extra_tags=draft.get("extra_tags", []))
        name = settings.calendar_name or "the calendar"
        op = {"op": "calendar_add", "path": "calendar", "calendar": name, "uid": f"gtd-{marker}", "summary": title,
              "start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes"),
              "alert_minutes": settings.calendar_alert_minutes, "rrule": None, "when": when}
        return [op], f"Event in {name}: {when}, alert {settings.calendar_alert_minutes} min before", title
    if route == "completion_report":
        if matched_task is None:
            raise ValueError("Completion needs a matching open item")
        _closable(matched_task)
        return ([{"op": "complete_task", "path": matched_task.path, "match": matched_task.raw,
                  "done": reference.isoformat()}], f"Tick in {matched_task.path.rsplit('/', 1)[-1]}",
                matched_task.title)
    raise ValueError(f"Unsupported route {route}")


def calendar_by_hand(vault: Vault, what: str, when: str, *, reference: date, marker: str,
                     extra_tags: list[str] | tuple[str, ...] = ()) -> tuple[list[dict[str, Any]], str, str]:
    """The agent can't write to the calendar (Proton Calendar is read only for it): an action to add the event
    by hand, due today, carrying the day, time and place."""
    name = "Proton Calendar" if vault.settings.calendar_source == "ics" else "your calendar"
    title = f"Add to {name}: {what}, {when}"
    hand = {"route": "next_action", "title": title, "context": "#computer", "project": None, "area": None,
            "dates": {"due": reference}, "extra_tags": list(extra_tags)}
    return compile_ops(vault, hand, capture=title, reference=reference, marker=marker)


def draft_from_interpretation(vault: Vault, value: dict[str, str], reference: date,
                              lookup: dict[str, Any]) -> tuple[dict[str, Any], str, Any]:
    """Convert validated model fields into a compile-ready draft plus extra assumptions."""
    assumptions = ""
    route = value["route"]
    someday = route == "someday_maybe" or (route == "new_project" and value["project_status"] == "someday")
    due = resolve_date_evidence(value["due_evidence"], reference)
    review, review_question = None, ""
    if someday:
        start = None  # for parked items the stated time is when to look again, not a start date
        if value["start_evidence"]:
            try:
                review = resolve_date_evidence(value["start_evidence"], reference)
            except ValueError:
                review_question = (f"'{value['start_evidence']}' is not a date. When should this come back? "
                                   "Set review: in the note, or give a date in Teacher feedback.")
                assumptions += f" Review date: {review_question}"
    else:
        start = resolve_date_evidence(value["start_evidence"], reference)
    since = resolve_date_evidence(value["since_evidence"], reference)
    if since and since > reference:
        raise ValueError("A waiting-since date cannot be in the future")
    if since and due and due < since:
        raise ValueError("The follow-up date comes before the waiting-since date")
    if start and due and due < start:
        raise ValueError("The due date comes before the start date")
    if any(value[f] and not re.search(r"\b\d{4}\b", value[f]) and re.search(r"\d", value[f])
           for f in ("since_evidence", "due_evidence", "start_evidence")):
        assumptions += f" Missing year uses {reference.year}."
    project = vault.projects.get(value["project_key"]) if value["project_key"] else None
    if route == "waiting_for" and not value["due_evidence"]:
        assumptions += " Follow-up defaults to seven days out."
    if value["time_estimate"] and route in {"next_action", "new_project"}:
        assumptions += f" Time estimate {value['time_estimate']} is a guess."
    title = value["title"]
    if "[[" in title or "]]" in title:
        raise ValueError("The title must not contain wikilinks")
    title = re.sub(r"\s+", " ", TAG_RE.sub("", plain_words(title))).strip()
    draft = {"route": route, "title": title, "project": project, "area": value["area_key"] or None,
             "context": value["context"], "person": value["person"], "what": value["what"],
             "dates": {"due": due, "start": start, "created": since, "review": review},
             "time_estimate": value["time_estimate"], "project_status": value["project_status"],
             "first_step": value["first_step"], "name": value["name_excerpt"], "extra_tags": [],
             "recurrence": value["recurrence"] or None, "review_question": review_question}
    if value["recurrence"] and not value["due_evidence"]:
        assumptions += " First due date is the next matching day."
    if route == "calendar_event":
        draft.update(event_day=due, start_time=resolve_time_evidence(value["time_evidence"]),
                     end_evidence=value["end_evidence"])
        if not value["end_evidence"]:
            assumptions += " No end given; the event lasts an hour."
    if route == "new_project":
        if not value["name_excerpt"] or value["name_excerpt"].casefold() in {"project", "new project"}:
            raise ValueError("A new project needs a meaningful name from the capture")
        if value["project_key"]:
            raise ValueError("A new project cannot reuse an existing project")
        step = value["first_step"]
        if re.match(r"(?i)^(?:create|make|set\s+up|start)\s+(?:a\s+)?(?:new\s+)?project\b", step):
            draft["first_step"] = ""
    matched = lookup.get(value["action_id"]) if route in {"completion_report", "waiting_for"} else None
    return draft, assumptions.strip(), matched


def ground_split(parent: str, items: list[str]) -> list[str]:
    """Keep a split only if every part reuses the capture's own words."""
    parent_words = set(normalize(parent).split())
    clean = []
    for item in items:
        item = re.sub(r"\s+", " ", item).strip()
        if not item or len(item) > 500:
            raise ValueError("Split item is empty or too long")
        words = [w for w in normalize(item).split() if len(w) > 2]
        if not words:
            raise ValueError("Split item has no content")
        overlap = sum(1 for w in words if w in parent_words) / len(words)
        if overlap < 0.6:
            raise ValueError("Split item adds words that are not in the capture")
        clean.append(item)
    return clean


def new_marker() -> str:
    return uuid.uuid4().hex[:10]
