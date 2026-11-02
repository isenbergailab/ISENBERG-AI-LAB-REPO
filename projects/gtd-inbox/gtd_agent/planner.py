"""The day's plan: the top tasks, each placed in a free gap, and the week's load.

Rules over the user's own vault and calendar. Nothing here calls a model, so private tasks are planned like any other.
The agent cannot write to Proton Calendar, so the blocks are an .ics file he imports himself.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

from .calendar_ics import Event, events_ics, free_gaps, overlapping
from .core import atomic_replace, destination_hash
from .dates import fmt_day
from .tasks import Task, actionable_rule
from .vault import Vault
from .workflow import _review_text, overlap_note

if TYPE_CHECKING:
    from .workflow import Agent

ROUND_MINUTES = 15  # a block starts on a quarter hour
PLAN = "plan|"      # ledger key prefix: one saved plan a day
PLAN_KEPT_DAYS = 14
PRIORITY_RANK = {"high": 0, "medium": 1, "": 1, "low": 2}


@dataclass(frozen=True)
class Want:
    """A task the plan would like to place, and how long it will take."""
    task: Task
    minutes: int
    guessed: bool  # the task has no time tag (#15m, #2h): the minutes are the default


@dataclass(frozen=True)
class Placed:
    want: Want
    start: datetime
    end: datetime

    @property
    def partial(self) -> bool:
        """The block is shorter than the task: a long task is planned in parts."""
        return self.end - self.start < timedelta(minutes=self.want.minutes)


def _round_up(moment: datetime) -> datetime:
    base = moment.replace(minute=0, second=0, microsecond=0)
    step = ROUND_MINUTES * 60
    return base + timedelta(seconds=math.ceil((moment - base).total_seconds() / step) * step)


def place(wants: list[Want], gaps: list[tuple[datetime, datetime]], limit: int,
          max_block: int) -> tuple[list[Placed], list[Want]]:
    """Put each task, in the order given, into the earliest gap that holds it, until `limit` are placed.
    Returns what was placed and what fitted nowhere (tasks after the limit are not tried)."""
    free = list(gaps)
    placed: list[Placed] = []
    left: list[Want] = []
    for want in wants:
        if len(placed) >= limit:
            break
        length = timedelta(minutes=min(want.minutes, max_block))
        for index, (begin, finish) in enumerate(free):
            start = _round_up(begin)
            if start + length <= finish:
                placed.append(Placed(want, start, start + length))
                free[index] = (start + length, finish)
                break
        else:
            left.append(want)
    return placed, left


def rank_key(task: Task, today: date, week_end: date, priority: str) -> tuple:
    """Most pressing first: overdue (oldest first), due today, scheduled for today or earlier, due this week,
    then the rest. Inside a group a high-priority project goes first, then the order in the vault."""
    due = task.due
    scheduled = task.dates.get("scheduled")
    if due and due < today:
        group, when = 0, due
    elif due == today:
        group, when = 1, due
    elif scheduled and scheduled <= today:
        group, when = 2, scheduled
    elif due and due <= week_end:
        group, when = 3, due
    else:
        group, when = 4, date.max
    return group, when, PRIORITY_RANK.get(priority, 1), task.path, task.line_no


def week_window(today: date) -> tuple[date, date, str]:
    """The days the load looks at: the rest of this week, and from Saturday the week ahead."""
    if today.weekday() >= 5:
        monday = today + timedelta(days=7 - today.weekday())
        return monday, monday + timedelta(days=6), "next week"
    return today, today + timedelta(days=6 - today.weekday()), "this week"


def _minutes_text(minutes: int) -> str:
    if minutes == 0:
        return "0h"
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest}m"
    return f"{hours}h" if not rest else f"{hours}h {rest}m"


def _why(task: Task, today: date) -> str:
    due = task.due
    scheduled = task.dates.get("scheduled")
    if due and due < today:
        days = (today - due).days
        return f"{days} day{'s' if days != 1 else ''} overdue"
    if due == today:
        return "due today"
    if scheduled and scheduled <= today and not due:
        return "scheduled today" if scheduled == today else f"scheduled {fmt_day(scheduled)}"
    if due:
        return f"due {fmt_day(due)}"
    return ""


@dataclass(frozen=True)
class Item:
    """One task in the plan, with what is needed to show it again later in the day."""
    key: str               # the task's key (note and words), to find it again
    title: str
    link: str              # the note it lives under, as [[LINK]]
    minutes: int           # what the whole task takes: its time tag, or the default
    guessed: bool
    start: str | None      # local time, ISO; None when the plan has no times
    end: str | None
    partial: bool          # the block is a part of the task
    why: str               # "3 days overdue", "due today", ...
    private: bool          # a private task: never named outside the vault


@dataclass(frozen=True)
class Plan:
    day: str               # ISO date
    made: str              # ISO date and time
    timed: bool            # False: the calendar was off or unreadable, so the top tasks have no times
    items: list[Item] = field(default_factory=list)
    left: list[Item] = field(default_factory=list)  # tasks near the top that fitted no free gap

    def dumps(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def loads(cls, text: str) -> "Plan":
        data = json.loads(text)
        return cls(data["day"], data["made"], data["timed"], [Item(**i) for i in data["items"]],
                   [Item(**i) for i in data["left"]])


class Planner:
    def __init__(self, agent: "Agent", read: Callable[[datetime, datetime], tuple[list[Event] | None, str | None]]
                 | None = None):
        """`read` is how the calendar is read (Housekeeper.calendar_read: one fetch a run); by default the agent's."""
        self.agent = agent
        self.settings = agent.settings
        self.ledger = agent.ledger
        self._read = read or agent.calendar_read

    # ------------------------------------------------------------ choosing
    def _workable(self, vault: Vault) -> list[Task]:
        """Open tasks that are the user's to do: not waiting on someone, not in a someday, done or dropped project and
        not blocked by a task that is still open."""
        open_ids = {t.task_id for t in vault.tasks if t.task_id and t.is_open}
        return [t for t in vault.tasks
                if t.is_open and not t.is_waiting and vault.status_for(t.path) not in {"someday", "done", "dropped"}
                and not any(i in open_ids for i in t.depends_on)]

    def _pool(self, vault: Vault, today: date) -> list[Task]:
        """Tasks the user could do today: next actions, single actions and anything due or scheduled by now.
        Not yet started and scheduled-for-later tasks are not in it."""
        found = []
        for task in self._workable(vault):
            status = vault.status_for(task.path)
            if task.start and task.start > today:
                continue
            scheduled = task.dates.get("scheduled")
            due_now = bool(task.due and task.due <= today)
            if scheduled and scheduled > today and not due_now:
                continue
            if actionable_rule(task, status, today, vault.settings.single_actions) or due_now or scheduled:
                found.append(task)
        return found

    def _wants(self, vault: Vault, today: date) -> list[Want]:
        week_end = today + timedelta(days=6 - today.weekday())

        def priority(task: Task) -> str:
            project = vault.project_for_path(task.path)
            return project.priority if project is not None else ""
        ranked = sorted(self._pool(vault, today), key=lambda t: rank_key(t, today, week_end, priority(t)))
        default = self.settings.plan_default_minutes
        return [Want(t, t.minutes or default, not t.minutes) for t in ranked]

    def _item(self, vault: Vault, want: Want, today: date, placed: Placed | None) -> Item:
        task = want.task
        return Item(task.key, _review_text(vault.display_title(task)), vault.where(task), want.minutes, want.guessed,
                    placed.start.isoformat(timespec="minutes") if placed else None,
                    placed.end.isoformat(timespec="minutes") if placed else None,
                    placed.partial if placed else False, _why(task, today), vault.is_private_task(task))

    def compute(self, vault: Vault, now: datetime, today: date, events: list[Event] | None) -> Plan:
        """The plan for today: the top tasks placed in free gaps, or the top tasks alone when `events` is None
        (the calendar is off or unreadable, so the free time is not known)."""
        s = self.settings
        wants = self._wants(vault, today)
        made = now.isoformat(timespec="seconds")
        if events is None:
            return Plan(today.isoformat(), made, False,
                        [self._item(vault, w, today, None) for w in wants[:s.plan_tasks]])
        gaps = free_gaps(events, today, s.day_start, s.day_end, now if now.date() == today else None,
                         s.min_free_minutes)
        placed, left = place(wants, gaps, s.plan_tasks, s.plan_max_block_minutes)
        items = [self._item(vault, p.want, today, p) for p in sorted(placed, key=lambda p: p.start)]
        return Plan(today.isoformat(), made, True, items,
                    [self._item(vault, w, today, None) for w in left[:s.plan_tasks]])

    # ------------------------------------------------------------ keeping
    def saved(self, day: date) -> Plan | None:
        text = self.ledger.kv_get(f"{PLAN}{day.isoformat()}")
        if not text:
            return None
        try:
            return Plan.loads(text)
        except (ValueError, KeyError, TypeError):
            return None

    def _today_events(self, today: date) -> list[Event] | None:
        start = datetime.combine(today, datetime.min.time())
        return self._read(start, start + timedelta(days=1))[0]

    def _keep(self, plan: Plan) -> None:
        self.ledger.kv_set(f"{PLAN}{plan.day}", plan.dumps())
        self._write_file(plan)

    def _write_file(self, plan: Plan) -> None:
        """The blocks as one calendar file. With no blocks there is no file, so an old one is never imported."""
        s = self.settings
        path = s.vault_path(s.blocks_file)
        if not (plan.timed and plan.items):
            if path.exists():
                os.unlink(path)
            return
        day = plan.day.replace("-", "")
        blocks = [(f"gtd-block-{day}-{item.key}", f"Do: {item.title}", datetime.fromisoformat(item.start),
                   datetime.fromisoformat(item.end)) for item in plan.items if item.start and item.end]
        text = events_ics(blocks, zone=s.timezone, alert_minutes=s.calendar_alert_minutes,
                          stamp=datetime.now(timezone.utc).replace(tzinfo=None))
        atomic_replace(path, text, s.state_dir, destination_hash(path), "\r\n", backup=False)

    def _prune(self, today: date) -> None:
        oldest = (today - timedelta(days=PLAN_KEPT_DAYS)).isoformat()
        for key in self.ledger.kv_items(PLAN):
            if key[len(PLAN):] < oldest:
                self.ledger.kv_delete(key)

    def run(self, vault: Vault, now: datetime | None = None) -> list[str]:
        """Housekeeping step: make today's plan once, from its hour, and write the calendar file. A plan with nothing
        placed is not kept, so the next check tries again (a task may arrive, or a gap open)."""
        s = self.settings
        now = now or datetime.now()
        today = self.agent.today()
        self._prune(today)
        if today.weekday() not in s.plan_days or now.hour < s.plan_hour or self.saved(today) is not None:
            return []
        events = self._today_events(today)
        if events is None:
            return []
        plan = self.compute(vault, now, today, events)
        if not plan.items:
            return []
        self._keep(plan)
        return [f"Planned {len(plan.items)} block{'s' if len(plan.items) != 1 else ''} for today"]

    def redo(self, vault: Vault, now: datetime | None = None) -> Plan:
        """Make today's plan again from now, whatever was kept."""
        now = now or datetime.now()
        today = self.agent.today()
        self.ledger.kv_delete(f"{PLAN}{today.isoformat()}")
        events = self._today_events(today)
        plan = self.compute(vault, now, today, events)
        if events is not None and plan.items:
            self._keep(plan)
        else:
            self._write_file(plan)
        return plan

    # ------------------------------------------------------------ load
    def load_text(self, vault: Vault, now: datetime, today: date) -> str:
        """'14h of work, 9h free this week.': what is due or scheduled by the end of the week against the free time
        in it, and by how much it is over. Tasks without a time tag count for the default minutes."""
        s = self.settings
        first, last, name = week_window(today)
        minutes = guessed = 0
        for task in self._workable(vault):
            scheduled = task.dates.get("scheduled")
            if (task.due and task.due <= last) or (scheduled and scheduled <= last):
                minutes += task.minutes or s.plan_default_minutes
                guessed += not task.minutes
        start = datetime.combine(first, datetime.min.time())
        events = self._read(start, start + timedelta(days=(last - first).days + 1))[0]
        if events is None:
            why = "calendar off" if not s.calendar_enabled else "calendar unavailable"
            text = f"{_minutes_text(minutes)} of work {name}. Free time unknown: {why}."
        else:
            free = 0
            for offset in range((last - first).days + 1):
                day = first + timedelta(days=offset)
                if day.weekday() in s.plan_days:
                    gaps = free_gaps(events, day, s.day_start, s.day_end, now if now.date() == day else None,
                                     s.min_free_minutes)
                    free += int(sum((end - begin).total_seconds() for begin, end in gaps) // 60)
            text = f"{_minutes_text(minutes)} of work, {_minutes_text(free)} free {name}."
            if minutes > free:
                text += f" Over by {_minutes_text(minutes - free)}: move or drop something."
        if guessed:
            text += f" Counted {guessed} task{'s' if guessed != 1 else ''} at {s.plan_default_minutes}m: no time tag."
        return text

    # ------------------------------------------------------------ showing
    def report(self, vault: Vault, now: datetime | None = None) -> list[str]:
        now = now or datetime.now()
        today = self.agent.today()
        return self.lines(vault, now, today, self._today_events(today))

    def current(self, vault: Vault, now: datetime, today: date, events: list[Event] | None) -> Plan | None:
        """Today's plan: the one kept, or, until it is made, the one the vault and calendar give right now (not
        kept). None when today is not a plan day, or the plan's hour has not come."""
        if today.weekday() not in self.settings.plan_days:
            return None
        plan = self.saved(today)
        if plan is None:
            if now.hour < self.settings.plan_hour:
                return None
            plan = self.compute(vault, now, today, events)
        return plan

    def lines(self, vault: Vault, now: datetime, today: date, events: list[Event] | None) -> list[str]:
        """The plan as lines for TODAY. `events` are today's, or None when the calendar is off or unreadable."""
        s = self.settings
        plan = self.current(vault, now, today, events)
        if plan is None:
            return []
        by_key: dict[str, list[Task]] = {}
        for task in vault.tasks:
            by_key.setdefault(task.key, []).append(task)
        if not plan.items:
            if plan.left:
                return ["- Plan for today: no free gap fits the top tasks.", *self._left(plan)]
            return ["- Plan for today: nothing to place. Add a `#next` step to an active project."]
        if not plan.timed:
            why = "calendar off" if not s.calendar_enabled else "calendar unavailable"
            head = f"- Top {len(plan.items)} for today (no times: {why}):"
        else:
            head = "- Plan for today:"
        lines = [head, *(f"  - {self._line(item, by_key, plan, now, events)}" for item in plan.items), *self._left(plan)]
        if plan.timed and s.vault_path(s.blocks_file).exists():
            lines.append(f"  - Calendar file: `{s.blocks_file}`. Import it into Proton Calendar.")
        return lines

    def _left(self, plan: Plan) -> list[str]:
        if not plan.left:
            return []
        return ["  - No free gap fits: " + ", ".join(f"{i.title} ({_minutes_text(i.minutes)})" for i in plan.left)]

    def _line(self, item: Item, by_key: dict[str, list[Task]], plan: Plan, now: datetime,
              events: list[Event] | None) -> str:
        day = date.fromisoformat(plan.day)
        same = by_key.get(item.key, [])
        finished = not any(t.is_open for t in same) or any(
            t.is_done and (t.done is None or t.done >= day) for t in same)
        head = item.title
        marker = ""
        clash = ""
        if item.start and item.end:
            start, end = datetime.fromisoformat(item.start), datetime.fromisoformat(item.end)
            head = f"{start:%H:%M}–{end:%H:%M} {item.title}"
            if finished:
                marker = "✓ "
            elif end < now:
                marker = "late "
            elif events is not None:
                found = overlapping(events, start, end)
                clash = overlap_note(found, day) if found else ""
        elif finished:
            marker = "✓ "
        if item.partial and item.start and item.end:
            size = _minutes_text(int((datetime.fromisoformat(item.end) - datetime.fromisoformat(item.start)).total_seconds() // 60))
            length = f"{size} of {_minutes_text(item.minutes)}"
        else:
            length = _minutes_text(item.minutes) + (" guessed" if item.guessed else "")
        parts = [f"{marker}{head}", length, item.why, item.link, clash]
        return " · ".join(part for part in parts if part)
