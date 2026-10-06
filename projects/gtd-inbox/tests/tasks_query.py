"""A small reading of the Obsidian Tasks query lines TODAY uses, to check which vault tasks each query shows.

Ticking a task inside a Tasks query changes its source line (that is the plugin's job), so TODAY is right when
every query selects exactly the source tasks it should. Unknown filter lines fail loudly.
"""
import re
from datetime import date, timedelta

from gtd_agent.tasks import Task


def _dates(field: str, rest: str, task: Task, today: date) -> bool:
    value = task.dates.get(field)
    words = {"today": today, "tomorrow": today + timedelta(days=1)}
    if rest == "today":
        return value == today
    match = re.fullmatch(r"(before|after) (today|tomorrow|in (\d+) days)", rest)
    if not match:
        raise ValueError(f"Unsupported date filter: {field} {rest}")
    target = words.get(match[2]) or today + timedelta(days=int(match[3]))
    if field == "start" and value is None:
        return True  # 'starts ...' filters also match tasks with no start date
    if value is None:
        return False
    return value < target if match[1] == "before" else value > target


def _filter(line: str, task: Task, today: date, open_ids: frozenset[str] = frozenset()) -> bool:
    line = line.strip()
    group = re.fullmatch(r"\((.+)\) OR \((.+)\)", line)
    if group:
        return _filter(group[1], task, today, open_ids) or _filter(group[2], task, today, open_ids)
    if line in {"is blocked", "is not blocked"}:  # blocked: it waits (⛔) on a task that is still open
        blocked = any(task_id in open_ids for task_id in task.depends_on)
        return blocked if line == "is blocked" else not blocked
    if line == "not done":
        return task.status in {" ", "/"}
    if line == "has start date":
        return task.start is not None
    if line == "no due date":
        return task.due is None
    match = re.fullmatch(r"tags (include|do not include) (#\S+)", line)
    if match:
        hit = any(match[2].casefold() in tag for tag in task.tags)  # Tasks matches tags by substring
        return hit if match[1] == "include" else not hit
    match = re.fullmatch(r"path regex (matches|does not match) /(.+)/([a-z]*)", line)
    if match:
        pattern = re.compile(match[2].replace("\\/", "/"), re.I if "i" in match[3] else 0)
        hit = pattern.search(task.path) is not None
        return hit if match[1] == "matches" else not hit
    match = re.fullmatch(r"path (includes|does not include) (\S+)", line)
    if match:
        hit = match[2].casefold() in task.path.casefold()
        return hit if match[1] == "includes" else not hit
    match = re.fullmatch(r"(due|starts) (.+)", line)
    if match:
        return _dates("due" if match[1] == "due" else "start", match[2], task, today)
    raise ValueError(f"Unsupported Tasks filter: {line}")


def select(query: str, tasks: list[Task], today: date) -> list[Task]:
    filters = [line for line in query.split("\n") if line.strip() and not line.startswith(("sort by", "limit",
                                                                                            "hide", "group by"))]
    open_ids = frozenset(task.task_id for task in tasks if task.task_id and task.status in {" ", "/"})
    return [task for task in tasks if all(_filter(line, task, today, open_ids) for line in filters)]


def queries(markdown: str) -> dict[str, str]:
    """Each ```tasks block in a note, keyed by the nearest heading above it (without its count)."""
    found, heading = {}, ""
    for block in re.finditer(r"^(#{2,3}) (.+?)$|^```tasks\n(.*?)^```", markdown, flags=re.M | re.S):
        if block[2]:
            heading = re.sub(r"\s*\(\d+\)$", "", block[2].strip())
        else:
            found[heading] = block[3]
    return found
