"""Semantic write operations. Each op is JSON data plus a pure text transform.

Ops are applied to the file as it is at approval time, so a note edited after a
proposal was made still receives exactly the reviewed line, in the reviewed place.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .markdown import HEADING_RE, section_bounds, walk
from .tasks import parse_task_line, with_date, with_status, with_tag, without_dependency, without_tag

# VAULT_RULES order: a missing section is inserted where this order puts it.
PROJECT_SECTION_ORDER = ("Outcome", "Why", "Context", "Current state", "Steps", "Deadlines", "Waiting For", "Decisions",
                         "Open questions", "Notes", "Log", "Links")


PREVIEW_LINES = 40


class OpError(RuntimeError):
    """The target no longer matches the reviewed change."""


def _lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return lines


def _join(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def _find_line(lines: list[str], match: str) -> int:
    hits = [i for i, line in enumerate(lines) if line.rstrip() == match.rstrip()]
    if not hits:
        raise OpError("The reviewed line is no longer in the note")
    if len(hits) > 1:
        raise OpError("The reviewed line appears more than once; edit one copy first")
    return hits[0]


def _content_end(lines: list[str], start: int, end: int) -> int:
    """Index after the last nonblank line in [start, end)."""
    position = end
    while position > start + 1 and not lines[position - 1].strip():
        position -= 1
    return position


def _ensure_section(lines: list[str], section: str, level: int = 2) -> tuple[list[str], tuple[int, int]]:
    bounds = section_bounds(lines, section, level)
    if bounds is not None:
        return lines, bounds
    heading = "#" * level + " " + section
    insert_at = len(lines)
    if section in PROJECT_SECTION_ORDER:
        later = PROJECT_SECTION_ORDER[PROJECT_SECTION_ORDER.index(section) + 1:]
        for name in later:
            found = section_bounds(lines, name, 2)
            if found is not None:
                insert_at = found[0]
                break
    block = [heading, ""]
    if insert_at > 0 and lines[insert_at - 1].strip():
        block = [""] + block
    lines = lines[:insert_at] + block + lines[insert_at:]
    return lines, section_bounds(lines, section, level)  # type: ignore[return-value]


def _done_split(lines: list[str], start: int, end: int) -> tuple[int, int | None]:
    """Split a section at its '### Done' subheading. Returns (end of the open part, Done heading index)."""
    for i in range(start + 1, end):
        heading = HEADING_RE.match(lines[i])
        if heading and len(heading.group(1)) >= 3 and heading.group(2).strip().casefold() == "done":
            return i, i
    return end, None


def _move_to_done(lines: list[str], op: dict[str, Any]) -> list[str]:
    section = op.get("section", "Steps")
    bounds = section_bounds(lines, section, 2)
    if bounds is None:
        raise OpError(f"The note has no {section} section")
    start, end = bounds
    top_end, done_at = _done_split(lines, start, end)
    hits = [i for i in range(start + 1, top_end) if lines[i].rstrip() == op["match"].rstrip()]
    if not hits:
        if done_at is not None and any(lines[i].rstrip() == op["match"].rstrip() for i in range(done_at, end)):
            return lines  # already moved
        raise OpError("The ticked step is no longer in the note")
    if len(hits) > 1:
        raise OpError("The ticked step appears more than once; edit one copy first")
    index = hits[0]
    task = parse_task_line(lines[index])
    if task is None or task.is_open:
        raise OpError("The step is no longer ticked")
    if index + 1 < len(lines) and lines[index + 1].startswith((" ", "\t")) and lines[index + 1].strip():
        raise OpError("The step has nested lines; move it by hand")
    line = lines.pop(index)
    start, end = section_bounds(lines, section, 2)  # type: ignore[misc]
    top_end, done_at = _done_split(lines, start, end)
    if done_at is None:
        insert_at = _content_end(lines, start, end)
        lines[insert_at:insert_at] = ["", "### Done", line]
        return lines
    first_item = next((i for i in range(done_at + 1, end) if re.match(r"^\s*[-*+] ", lines[i])), None)
    lines.insert(first_item if first_item is not None else done_at + 1, line)
    return lines


def _insert_task(lines: list[str], op: dict[str, Any]) -> list[str]:
    section = op["section"]
    level = int(op.get("level", 2))
    lines, (start, end) = _ensure_section(lines, section, level)
    position = op.get("position", "end")
    new_line = op["line"]
    if position == "start":
        index = start + 1
        while index < end and lines[index].strip() == "":
            index += 1
        # Keep an intro paragraph above the list: insert before the first list item.
        first_item = next((i for i in range(start + 1, end) if re.match(r"^\s*[-*+] ", lines[i])), None)
        index = first_item if first_item is not None else _content_end(lines, start, end)
        lines.insert(index, new_line)
        return lines
    if position == "queue":
        top_end, _ = _done_split(lines, start, end)
        last_task = None
        for i in range(start + 1, top_end):
            task = parse_task_line(lines[i])
            if task is not None and not task.indent:
                last_task = i
        if last_task is not None:
            index = last_task + 1
            while index < top_end and lines[index].startswith((" ", "\t")) and lines[index].strip():
                index += 1  # keep a task's nested children with it
        else:
            index = _content_end(lines, start, top_end)
            placeholder = next((i for i in range(start + 1, top_end) if re.fullmatch(r"_.*_", lines[i].strip())), None)
            if placeholder is not None:
                lines[placeholder] = new_line
                return lines
        lines.insert(index, new_line)
        return lines
    if position == "steps":
        infos = list(walk("\n".join(lines)))
        tasks = []
        done_heading = None
        for i in range(start + 1, end):
            info = infos[i]
            if info.heading_level >= 3 and info.heading.casefold() == "done":
                done_heading = i
                break
            task = parse_task_line(lines[i])
            if task is not None and not info.in_code and not task.indent:
                tasks.append((i, task))
        open_next = [i for i, task in tasks if task.is_open and task.is_next and not task.is_waiting]
        open_any = [i for i, task in tasks if task.is_open and not task.is_waiting]
        if open_next:
            index = open_next[-1] + 1
            while index < len(lines) and lines[index].startswith((" ", "\t")) and lines[index].strip():
                index += 1  # keep a task's nested children with it
        elif open_any:
            index = open_any[0]
        else:
            stop = done_heading if done_heading is not None else end
            index = _content_end(lines, start, stop)
            placeholder = next((i for i in range(start + 1, stop) if lines[i].strip().startswith("_No steps")), None)
            if placeholder is not None:
                lines[placeholder] = new_line
                return lines
        lines.insert(index, new_line)
        return lines
    index = _content_end(lines, start, end)
    placeholder = next((i for i in range(start + 1, end) if re.fullmatch(r"_.*_", lines[i].strip())), None)
    if placeholder is not None and index == placeholder + 1 and op.get("replace_placeholder", True):
        lines[placeholder] = new_line
        return lines
    lines.insert(index, new_line)
    return lines


def _add_decision(lines: list[str], op: dict[str, Any]) -> list[str]:
    """Add a decision line to a Decisions section, a chosen section of a decisions note, or above its first ## heading."""
    line = op["line"]
    if any(existing.rstrip() == line.rstrip() for existing in lines):
        return lines  # already applied
    section = op.get("section")
    if section:
        if op.get("ensure"):
            lines, (start, end) = _ensure_section(lines, section, 2)
        else:
            bounds = section_bounds(lines, section, 2)
            if bounds is None:
                raise OpError(f"The note has no {section} section")
            start, end = bounds
        top_end = next((i for i in range(start + 1, end)
                        if (m := HEADING_RE.match(lines[i])) and len(m.group(1)) >= 3), end)
        placeholder = next((i for i in range(start + 1, top_end) if re.fullmatch(r"_.*_", lines[i].strip())), None)
        if placeholder is not None:
            lines[placeholder] = line
            return lines
        index = _content_end(lines, start, top_end)
    else:
        first_h2 = next((i for i, text in enumerate(lines) if re.match(r"^##\s", text)), len(lines))
        index = _content_end(lines, 0, first_h2)
    before = lines[index - 1] if index > 0 else ""
    block = [line]
    if before.strip() and not re.match(r"^\s*[-*+] ", before) and not HEADING_RE.match(before):
        block = ["", line]
    if index < len(lines) and lines[index].strip() and HEADING_RE.match(lines[index]):
        block = block + [""]
    lines[index:index] = block
    return lines


def _set_property(lines: list[str], op: dict[str, Any]) -> list[str]:
    """Set one frontmatter property, such as status or due. An empty value clears it."""
    line = f"{op['key']}: {op['value']}".rstrip()
    if not lines or lines[0].strip() != "---":
        raise OpError("The note has no properties block")
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        raise OpError("The properties block is not closed")
    for index in range(1, end):
        if re.match(re.escape(op["key"]) + r"\s*:", lines[index]):
            lines[index] = line
            return lines
    lines.insert(end, line)
    return lines


def section_text(lines: list[str], name: str) -> str | None:
    bounds = section_bounds(lines, name, 2)
    if bounds is None:
        return None
    return "\n".join(line.rstrip() for line in lines[bounds[0] + 1:bounds[1]]).strip()


def _set_section(lines: list[str], op: dict[str, Any]) -> list[str]:
    """Replace a section's text (the heading stays). It must still hold what the proposal saw ('old')."""
    bounds = section_bounds(lines, op["section"], 2)
    if bounds is None:
        raise OpError(f"The note has no {op['section']} section")
    current = section_text(lines, op["section"])
    if current == op["text"].strip():
        return lines  # already applied
    if "old" in op and current != op["old"].strip():
        raise OpError(f"The {op['section']} section changed since")
    start, end = bounds
    return lines[:start + 1] + op["text"].strip().split("\n") + lines[_content_end(lines, start, end):]


def _append_bullet(lines: list[str], op: dict[str, Any]) -> list[str]:
    section = op.get("section")
    bullet = f"- {op['text']}"
    if not section:
        lines = list(lines)
        while lines and not lines[-1].strip():
            lines.pop()
        return lines + [bullet]
    return _insert_task(lines, {"section": section, "line": bullet, "position": op.get("position", "end"),
                                "level": op.get("level", 2), "replace_placeholder": True})


def apply_op(text: str, op: dict[str, Any], today: date) -> str:
    kind = op["op"]
    if kind == "create_file":
        if text:
            raise OpError("File already exists")
        return op["text"] if op["text"].endswith("\n") else op["text"] + "\n"
    lines = _lines(text)
    if kind == "add_task":
        marker = op.get("marker")
        if marker and any(marker in line for line in lines):
            return _join(lines)  # already applied
        return _join(_insert_task(lines, op))
    if kind == "append_log":
        entry = f"- {op['entry']}"
        if entry in lines:
            return _join(lines)
        return _join(_insert_task(lines, {"section": "Log", "line": entry, "position": "start"}))
    if kind == "append_bullet":
        if f"- {op['text']}" in lines:
            return _join(lines)
        return _join(_append_bullet(lines, op))
    if kind in {"complete_task", "cancel_task", "add_tag", "remove_tag", "set_date", "remove_dependency",
                "replace_line"}:
        expected = expected_line(op, today)
        if expected in [line.rstrip() for line in lines] and op["match"].rstrip() not in [l.rstrip() for l in lines]:
            return _join(lines)  # already applied
        index = _find_line(lines, op["match"])
        if kind in {"complete_task", "cancel_task"}:
            task = parse_task_line(lines[index])
            if task is None or not task.is_open:
                raise OpError("The task is no longer open")
        lines[index] = expected
        return _join(lines)
    if kind == "replace_link":
        old, new = op["old"], op["new"]
        pattern = re.compile(r"\[\[" + re.escape(old) + r"(#[^\]|]*)?(\|[^\]]*)?\]\]")
        changed = False

        def fix(match: re.Match) -> str:
            nonlocal changed
            changed = True
            heading = match.group(1) or ""
            alias = match.group(2) or ("|" + old if re.search(r"[a-z ]", old) else "")
            return f"[[{new}{heading}{alias}]]"
        updated = [pattern.sub(fix, line) for line in lines]
        if not changed and not any(f"[[{new}" in line for line in lines):
            raise OpError("The broken link is no longer in the note")
        return _join(updated)
    if kind == "move_to_done":
        return _join(_move_to_done(lines, op))
    if kind == "add_decision":
        return _join(_add_decision(lines, op))
    if kind == "remove_line":
        hits = [i for i, line in enumerate(lines) if line.rstrip() == op["match"].rstrip()]
        occurrence = int(op.get("occurrence", 2))
        if len(hits) < occurrence:
            return _join(lines)  # already removed
        del lines[hits[occurrence - 1]]
        return _join(lines)
    if kind == "set_property":
        return _join(_set_property(lines, op))
    if kind == "set_section":
        return _join(_set_section(lines, op))
    if kind in {"remove_file", "move_file"}:
        raise OpError(f"{kind} is applied by the agent, not as a text edit")
    raise OpError(f"Unknown operation {kind}")


def expected_line(op: dict[str, Any], today: date) -> str:
    kind = op["op"]
    match = op["match"]
    if kind == "complete_task":
        done = date.fromisoformat(op.get("done") or today.isoformat())
        return with_status(match, "x", done).rstrip()
    if kind == "cancel_task":
        return with_status(match, "-", date.fromisoformat(op.get("done") or today.isoformat())).rstrip()
    if kind == "add_tag":
        return with_tag(match, op["tag"]).rstrip()
    if kind == "remove_tag":
        return without_tag(match, op["tag"]).rstrip()
    if kind == "set_date":
        return with_date(match, op["field"], date.fromisoformat(op["value"])).rstrip()
    if kind == "remove_dependency":
        return without_dependency(match, op["id"]).rstrip()
    if kind == "replace_line":
        return op["line"].rstrip()
    raise OpError("No expected line for this operation")


def describe(op: dict[str, Any], today: date) -> str:
    kind = op["op"]
    path = op["path"]
    if kind == "add_task":
        return f"{path} › {op['section']}\n+ {op['line']}"
    if kind == "append_log":
        return f"{path} › Log\n+ - {op['entry']}"
    if kind == "append_bullet":
        where = f" › {op['section']}" if op.get("section") else ""
        return f"{path}{where}\n+ - {op['text']}"
    if kind in {"complete_task", "cancel_task", "add_tag", "remove_tag", "set_date", "remove_dependency",
                "replace_line"}:
        return f"{path}\n- {op['match'].strip()}\n+ {expected_line(op, today).strip()}"
    if kind == "set_property":
        return f"{path} › properties\n+ {op['key']}: {op['value']}".rstrip()
    if kind == "set_section":
        old = "".join(f"\n- {line}" for line in op.get("old", "").split("\n") if line.strip())
        return f"{path} › {op['section']}{old}" + "".join(f"\n+ {line}" for line in op["text"].strip().split("\n"))
    if kind == "mail_draft":
        return f"Draft into your Proton Drafts\nTo: {op.get('to') or '(add the address)'} · Subject: {op['subject']}"
    if kind == "create_file":
        lines = op["text"].rstrip("\n").split("\n")
        shown = "\n".join("+ " + line for line in lines[:PREVIEW_LINES])
        if len(lines) > PREVIEW_LINES:
            shown += f"\n+ ... ({len(lines) - PREVIEW_LINES} more lines)"
        return f"New file {path}\n" + shown
    if kind == "remove_file":
        return f"Remove {path} from the Inbox (the copy above replaces it; a backup stays in agent state)"
    if kind == "move_file":
        return f"Move {path} to {op['to']}"
    if kind == "replace_link":
        return f"{path}\n- [[{op['old']}]]\n+ [[{op['new']}]]"
    if kind == "remove_line":
        which = op.get("note") or ("second copy" if int(op.get("occurrence", 2)) == 2 else "moved elsewhere")
        return f"{path}\n- {op['match'].strip()}  ({which})"
    if kind == "move_to_done":
        return f"{path} › {op.get('section', 'Steps')} › Done\n~ {op['match'].strip()}"
    if kind == "add_decision":
        return f"{path} › {op.get('section') or 'top of the note'}\n+ {op['line']}"
    if kind == "calendar_add":
        repeat = f" · repeats {op['repeat']}" if op.get("repeat") else ""
        return (f"Calendar {op['calendar']}\n+ {op['when']} {op['summary']}{repeat} · alert "
                f"{op['alert_minutes']} min before")
    return f"{path}: {kind}"


def op_paths(ops: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for op in ops:
        if op["path"] not in seen:
            seen.append(op["path"])
    return seen


def heading_exists(text: str, name: str, level: int = 2) -> bool:
    for line in text.split("\n"):
        match = HEADING_RE.match(line)
        if match and len(match.group(1)) == level and match.group(2).strip().casefold() == name.casefold():
            return True
    return False
