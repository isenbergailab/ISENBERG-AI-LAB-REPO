"""Phone pings: what the agent sends you on Telegram on its own, besides its answers to your messages.

Three kinds, each on its own rule, and quiet hours hold all of them:
- the morning brief: the day's calendar and plan, once a day;
- approvals waiting: things in APPROVAL that have waited for you;
- mail down: Bridge (or the mail connection) has stayed down.

Telegram does not encrypt messages end to end, so a private task, a private event or a private proposal is never
named, and with `titles = false` nothing is named at all.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any

from .calendar_ics import Event, event_span
from .clarification import ROUTE_LABEL
from .dates import fmt_day
from .planner import Plan, Planner
from .telegram import TelegramCapture
from .vault import Vault
from .workflow import Agent

BRIEF = "ping|brief"          # ledger key: the day of the last brief
BRIEF_HOURS = 6               # the brief is for the morning: it is not sent this long after its time
LISTED_EVENTS = 8             # events named in the brief; the rest are counted
APPROVALS = "ping|approvals"  # ledger key: {"at": when you were last told, "keys": what was waiting then}
LISTED = 5                    # approvals named in one ping; the rest are counted
TITLE_CHARS = 60
REMIND_HOURS = 24             # with nothing new waiting, the reminder comes this often
# Proposals from these origins take their title from outside the vault (a subject, a file name, a contact), so
# the ping gives their kind only.
OUTSIDE = {"mail", "document", "contact"}
BRIDGE = "ping|bridge"  # ledger key: {"since": when mail was first seen down, "told": whether you were told}
BRIDGE_DOWN = ("Proton Bridge is not answering, so mail is not being read. "
               "Open Bridge on the laptop and leave it running.")
MAIL_DOWN = "Mail is not being read. TODAY on the laptop says what to fix."
MAIL_BACK = "Mail is being read again."


def _line(text: Any) -> str:
    """Words as one plain line. Telegram shows text as it is, so nothing is escaped for Markdown."""
    return " ".join(str(text).split())


def _strings(value: Any):
    """Every string inside a proposal, however deep."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def quiet(now: datetime, start: tuple[int, int], end: tuple[int, int]) -> bool:
    """Whether `now` falls in the quiet window [start, end). A window may cross midnight; one that starts where it
    ends is never quiet."""
    moment = (now.hour, now.minute)
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


class Pings:
    """The housekeeping step that sends the pings. A ping is marked sent only after Telegram took it, so one that
    could not be sent is tried again at the next check."""

    def __init__(self, housekeeper: Any) -> None:
        self.hk = housekeeper
        self.agent = housekeeper.agent
        self.settings = housekeeper.settings
        self.ledger = housekeeper.ledger
        self.telegram = TelegramCapture(self.agent)

    def run(self, vault: Any, now: datetime | None = None) -> list[str]:
        s = self.settings
        now = now or datetime.now()
        if not s.telegram_pings or not self.telegram.ready():
            return []
        held = quiet(now, s.telegram_quiet_start, s.telegram_quiet_end)
        messages: list[str] = []
        brief = self._brief(vault, now, held) if "brief" in s.telegram_pings else []
        messages += brief
        if brief:
            self._told_approvals(now)  # the brief counted what waits
        elif "approvals" in s.telegram_pings:
            messages += self._approvals(vault, now, held)
        if "bridge" in s.telegram_pings:
            messages += self._bridge(now, held)
        return messages

    # ------------------------------------------------------------ morning brief
    def _brief(self, vault: Vault, now: datetime, held: bool) -> list[str]:
        """The day's calendar, plan and what needs you: once a day, from the brief time and for a few hours."""
        today = self.agent.today()
        begins = datetime.combine(today, time(*self.settings.telegram_brief))
        if (held or self.ledger.kv_get(BRIEF) == today.isoformat()
                or not begins <= now < begins + timedelta(hours=BRIEF_HOURS)):
            return []
        self.telegram.send(self._brief_text(vault, now, today))
        self.ledger.kv_set(BRIEF, today.isoformat())
        return ["Telegram: sent the morning brief"]

    def _brief_text(self, vault: Vault, now: datetime, today: date) -> str:
        planner = Planner(self.agent, self.hk.calendar_read)
        day_start = datetime.combine(today, datetime.min.time())
        events = self.hk.calendar_read(day_start, day_start + timedelta(days=1))[0]
        due, follow = len(self.hk.due_tasks(vault, today)), len(self.hk.follow_ups_due(vault, today))
        counts = [f"{label}: {count}" for label, count in (
            ("Due or overdue", due), ("Follow-ups due", follow), ("APPROVAL", self.ledger.count_pending()),
            ("Inbox", self.hk.inbox_count())) if count]
        blocks = [[fmt_day(today)], self._calendar_block(events, day_start),
                  self._plan_block(planner.current(vault, now, today, events)),
                  [" · ".join(counts) if counts else "Nothing is due or waiting.", planner.load_text(vault, now, today)]]
        return "\n\n".join("\n".join(block) for block in blocks if block)

    def _calendar_block(self, events: list[Event] | None, day_start: datetime) -> list[str]:
        if not self.settings.calendar_enabled:
            return []
        if events is None:
            return ["Calendar: unavailable"]
        if not events:
            return ["Calendar: nothing scheduled"]
        if not self.settings.telegram_titles:
            return [f"Calendar: {len(events)} event{'s' if len(events) != 1 else ''}"]
        ordered = [e for e in events if e.all_day] + [e for e in events if not e.all_day]
        lines = [f"{'all day' if e.all_day else event_span(e, day_start)} {self._words(e.summary, e.private, 'event')}"
                 for e in ordered[:LISTED_EVENTS]]
        if len(ordered) > LISTED_EVENTS:
            lines.append(f"…and {len(ordered) - LISTED_EVENTS} more")
        return ["Calendar", *lines]

    def _plan_block(self, plan: Plan | None) -> list[str]:
        if plan is None:
            return []
        if not plan.items:
            return ["Plan: no free gap fits the top tasks"] if plan.left else []
        count = len(plan.items)
        if not self.settings.telegram_titles:
            if plan.timed:
                return [f"Plan: {count} block{'s' if count != 1 else ''}, first at {plan.items[0].start[11:16]}"]
            return [f"Plan: {count} top task{'s' if count != 1 else ''}"]
        lines = []
        for item in plan.items:
            when = f"{item.start[11:16]}–{item.end[11:16]} " if item.start and item.end else ""
            lines.append(f"{when}{self._words(item.title, item.private, 'task')}" + (f" · {item.why}" if item.why else ""))
        return ["Plan" if plan.timed else "Top tasks", *lines]

    @staticmethod
    def _words(text: str, private: bool, noun: str) -> str:
        """What the brief calls a thing: its words cut to a line, or 'A private task' when it is private."""
        if private:
            return f"A private {noun}"
        text = _line(text)
        return text if len(text) <= TITLE_CHARS else text[:TITLE_CHARS - 1] + "…"

    def _told_approvals(self, now: datetime) -> None:
        """The brief counted what waits in APPROVAL, so an approvals ping now would only repeat it."""
        rows = self.ledger.pending()
        if rows:
            self.ledger.kv_set(APPROVALS, json.dumps({"at": now.isoformat(timespec="seconds"),
                                                      "keys": sorted(row["dedupe_key"] for row in rows)}))

    # ------------------------------------------------------------ approvals
    def _approvals(self, vault: Vault, now: datetime, held: bool) -> list[str]:
        """Approvals that have waited the set hours. A new one is told after the gap since the last ping; with
        nothing new, the reminder comes once a day. What you decide leaves the list."""
        s = self.settings
        cutoff = (now - timedelta(hours=s.telegram_approvals_hours)).isoformat(timespec="seconds")
        waiting = [row for row in self.ledger.pending() if row["updated_at"] <= cutoff]
        if not waiting or held:
            return []
        keys = sorted(row["dedupe_key"] for row in waiting)
        last = json.loads(self.ledger.kv_get(APPROVALS) or "{}")
        if last:
            since = now - datetime.fromisoformat(last["at"])
            new = bool(set(keys) - set(last["keys"]))
            if not (new and since >= timedelta(hours=s.telegram_approvals_gap_hours)
                    or since >= timedelta(hours=REMIND_HOURS)):
                return []
        self.telegram.send(self._approvals_text(vault, waiting))
        self.ledger.kv_set(APPROVALS, json.dumps({"at": now.isoformat(timespec="seconds"), "keys": keys}))
        count = len(waiting)
        return [f"Telegram: told you {count} approval{'s' if count != 1 else ''} wait{'' if count != 1 else 's'}"]

    def _approvals_text(self, vault: Vault, rows: list[Any]) -> str:
        count = len(rows)
        waits = "1 thing waits" if count == 1 else f"{count} things wait"
        if not self.settings.telegram_titles:
            return f"{waits} for your OK in APPROVAL. Open it to decide."
        lines = [f"{waits} for your OK in APPROVAL:", *(f"- {self._named(vault, row)}" for row in rows[:LISTED])]
        if count > LISTED:
            lines.append(f"- …and {count - LISTED} more")
        return "\n".join(lines + ["Open APPROVAL to decide."])

    def _named(self, vault: Vault, row: Any) -> str:
        """One approval as the ping names it: its kind, and its title unless that is private or from outside."""
        proposal = json.loads(row["proposal_json"])
        kind = str(proposal.get("kind", ""))
        label = ROUTE_LABEL.get(kind, kind.replace("_", " ").capitalize())
        if row["origin"] in OUTSIDE:
            return label
        if self._private(vault, proposal):
            return f"{label} · A private item"
        title = _line(proposal.get("title") or proposal.get("source") or "")
        if not title:
            return label
        return f"{label} · {title if len(title) <= TITLE_CHARS else title[:TITLE_CHARS - 1] + '…'}"

    @staticmethod
    def _private(vault: Vault, proposal: dict[str, Any]) -> bool:
        """A proposal that is marked private, mentions something private (a tag, a link to a private project or
        area) or files into a private project or area."""
        if proposal.get("private") or any(Agent._is_private_text(vault, text) for text in _strings(proposal)):
            return True
        for op in proposal.get("ops") or []:
            path = str(op.get("path", "")) if isinstance(op, dict) else ""
            project, area = vault.project_for_path(path), vault.area_for_path(path)
            if (project is not None and vault.is_private_project(project)) or (area is not None and area.private):
                return True
        return False

    # ------------------------------------------------------------ mail down
    def _bridge(self, now: datetime, held: bool) -> list[str]:
        """Mail that stays down is told once; when it works again, that is told too. The clock runs in quiet
        hours as well: only the sending waits. The ping names no details: they can hold your address."""
        s = self.settings
        if not s.mail_enabled:
            return []
        problem = self.ledger.kv_get("mail_problem") or ""
        state = json.loads(self.ledger.kv_get(BRIDGE) or "{}")
        if problem:
            if not state:
                state = {"since": now.isoformat(timespec="seconds"), "told": False}
                self.ledger.kv_set(BRIDGE, json.dumps(state))
            lasted = now >= datetime.fromisoformat(state["since"]) + timedelta(minutes=s.telegram_bridge_minutes)
            if state["told"] or held or not lasted:
                return []
            self.telegram.send(BRIDGE_DOWN if self.ledger.kv_get("mail_closed") else MAIL_DOWN)
            self.ledger.kv_set(BRIDGE, json.dumps({**state, "told": True}))
            return ["Telegram: told you mail is down"]
        if not state:
            return []
        if state["told"]:
            if held:
                return []
            self.telegram.send(MAIL_BACK)
            self.ledger.kv_delete(BRIDGE)
            return ["Telegram: told you mail is back"]
        self.ledger.kv_delete(BRIDGE)
        return []
