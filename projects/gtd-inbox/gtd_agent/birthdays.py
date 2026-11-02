"""Local, review-first birthday reminders from the read-only calendar."""
from __future__ import annotations

import calendar
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .calendar_ics import Event
from .core import process_lock
from .tasks import format_task
from .workflow import PROPOSAL_VERSION, Agent


BIRTHDAY_TITLE = re.compile(r"^(?P<name>.+?)(?:['’]s)?\s+(?:birthday|b[ -]?day)$", re.IGNORECASE)
SAFE_NAME = re.compile(r"^[^\W\d_][\w .’'\-]{0,119}$", re.UNICODE)
ACTION_TITLE = re.compile(r"^(?:ask|buy|send|call|text|message|plan|book|order|pick|remind|schedule|confirm)\b",
                          re.IGNORECASE)
MARKER = re.compile(r"\^gtd-bday-[0-9a-f]{12}\b")


@dataclass(frozen=True)
class BirthdayScan:
    count: int
    flagged: list[str]
    proposal_id: str | None = None
    warning: str | None = None
    already_recorded: int = 0


def _next_birthday(today: date, month: int, day: int) -> date:
    for year in range(today.year, today.year + 5):
        try:
            candidate = date(year, month, day)
        except ValueError:  # February 29 in a non-leap year
            continue
        if candidate >= today:
            return candidate
    raise ValueError("No next birthday date")


class BirthdayImporter:
    def __init__(self, agent: Agent,
                 reader: Callable[[datetime, datetime], tuple[list[Event] | None, str | None]] | None = None):
        self.agent = agent
        if reader is None:
            from .housekeeping import Housekeeper
            reader = Housekeeper(agent).calendar_read
        self.reader = reader

    def scan(self, preferred_dates: dict[str, str] | None = None) -> BirthdayScan:
        """Queue one reviewed proposal; never change a task or contact directly."""
        today = self.agent.today()
        start = datetime.combine(today - timedelta(days=365), datetime.min.time())
        end = datetime.combine(today + timedelta(days=366), datetime.min.time())
        events, warning = self.reader(start, end)
        if events is None:
            return BirthdayScan(0, [], warning=warning or "Calendar unavailable")
        preferred_dates = {key.casefold().strip(): value for key, value in (preferred_dates or {}).items()}
        for value in preferred_dates.values():
            if not re.fullmatch(r"\d{2}-\d{2}", value):
                raise ValueError("Birthday choices use MM-DD")
            date.fromisoformat("2000-" + value)

        by_name: dict[str, list[tuple[str, int, int, str]]] = defaultdict(list)
        for event in events:
            if event.private:
                continue
            match = BIRTHDAY_TITLE.fullmatch(event.summary.strip())
            if match is None:
                continue  # birthday shopping, parties and questions are not birth dates
            name = " ".join(match.group("name").split())
            if not SAFE_NAME.fullmatch(name) or ACTION_TITLE.match(name):
                continue  # calendar text never becomes untrusted Markdown or a Tasks tag
            by_name[name.casefold()].append((name, event.start.month, event.start.day, event.uid))

        # A first-name-only entry may name the same person as a full-name entry.
        # Different full names with the same first name are kept separate.
        clusters: list[list[tuple[str, int, int, str]]] = []
        joined: set[str] = set()
        for key in sorted(by_name):
            if key in joined:
                continue
            first = key.split()[0]
            family = [key]
            if key == first:
                family += [other for other in by_name if other != key and other.split()[0] == first]
            elif first in by_name and first not in joined:
                family = [first] + [other for other in by_name if other != first and other.split()[0] == first]
            joined.update(family)
            clusters.append([item for name in family for item in by_name[name]])

        flagged: list[str] = []
        birthdays: list[tuple[str, int, int]] = []
        for cluster in clusters:
            dates = {(month, day) for _, month, day, _ in cluster}
            names = {name.casefold() for name, _, _, _ in cluster}
            first = min(names, key=len).split()[0]
            choice = next((preferred_dates[key] for key in (first, *sorted(names)) if key in preferred_dates), None)
            if len(dates) > 1 and choice is None:
                by_uid: dict[str, set[tuple[int, int]]] = defaultdict(set)
                for _, month, day, uid in cluster:
                    by_uid[uid].add((month, day))
                display = min((name for name, _, _, _ in cluster), key=len)
                detail = "calendar repeats on multiple dates" if any(len(days) > 1 for days in by_uid.values()) else \
                    "conflicting calendar dates"
                flagged.append(f"{display}: {detail}")
                continue
            selected = tuple(map(int, choice.split("-"))) if choice else next(iter(dates))
            if selected not in dates:
                raise ValueError(f"Chosen birthday {choice} is not in the calendar")
            names_on_date = {name for name, month, day, _ in cluster if (month, day) == selected}
            name = max(names_on_date, key=len)
            birthdays.append((name, *selected))

        existing_tasks = [task for task in self.agent.vault().tasks if task.is_open]
        markers = {"^" + task.block_id for task in existing_tasks
                   if task.block_id and MARKER.fullmatch("^" + task.block_id)}
        items: list[dict[str, Any]] = []
        already_recorded = 0
        for name, month, day in sorted(birthdays, key=lambda row: (_next_birthday(today, row[1], row[2]), row[0])):
            marker = "^gtd-bday-" + hashlib.sha256(
                f"{name.casefold()}|{month:02d}-{day:02d}".encode("utf-8")).hexdigest()[:12]
            found = marker in markers or any(
                task.due is not None and (task.due.month, task.due.day) == (month, day)
                and re.search(r"\b(?:birthday|b[ -]?day)\b", task.title, re.IGNORECASE)
                and re.search(r"\b" + re.escape(name.casefold()) + r"\b", task.title.casefold())
                for task in existing_tasks)
            if found:
                already_recorded += 1
                continue
            due = _next_birthday(today, month, day)
            line = format_task(f"Message {name} on birthday", ["#anywhere"],
                               dates={"start": due, "due": due, "created": today},
                               recurrence="every year", block_id=marker[1:])
            items.append({"label": f"{name} · {calendar.month_abbr[month]} {day} · message on birthday",
                          "ops": [{"op": "add_task", "path": self.agent.settings.single_actions,
                                   "section": "Birthdays", "position": "end", "line": line, "marker": marker}]})

        if not items:
            return BirthdayScan(0, sorted(flagged), warning=warning, already_recorded=already_recorded)
        fingerprint = hashlib.sha256("|".join(item["ops"][0]["marker"] for item in items)
                                     .encode("utf-8")).hexdigest()[:16]
        key = "birthday-import|" + fingerprint
        with process_lock(self.agent.settings.state_dir):
            existing_row = self.agent.ledger.get_by_key(key)
            if existing_row is not None:
                return BirthdayScan(len(items), sorted(flagged),
                                    existing_row["id"] if existing_row["status"] == "pending" else None,
                                    warning, already_recorded)
            ops = [op for item in items for op in item["ops"]]
            proposal = {"kind": "birthday_import", "title": "Birthday message reminders",
                        "summary": f"{len(items)} annual reminders in Single actions › Birthdays",
                        "reason": (f"Read-only Proton Calendar scan, {start.date()} through {(end - timedelta(days=1)).date()}. "
                                   "Message tasks appear on the birthday. No message is sent. "
                                   + ("Needs date review: " + "; ".join(sorted(flagged)) if flagged else "")),
                        "engine_version": PROPOSAL_VERSION, "items": items, "base_ops": [], "ops": ops,
                        "flagged": sorted(flagged)}
            proposal_id = self.agent.ledger.upsert("birthday", key, proposal)
            self.agent.render_reviews()
        return BirthdayScan(len(items), sorted(flagged), proposal_id, warning, already_recorded)
