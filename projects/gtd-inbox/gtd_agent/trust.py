"""The trust report (phase 3 item 4): per kind of proposal, what you decided in a week, and which kinds earned
auto-apply. It reads the ledger and suggests; `[trust] bookkeeping` in config.toml stays yours to change. It shows
counts of kinds only, never what a proposal said, so private items reveal nothing here."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .clarification import ROUTE_LABEL
from .core import TRUSTABLE

LOOKBACK_DAYS = 28
SUGGEST_AFTER = 8  # approvals as written, in the lookback, before a kind is suggested...
SUGGEST_DAYS = 3  # ...on at least this many different days: one sitting of ticks is not a track record
QUESTIONS = ("manual", "drop_reason", "after_event", "lint_choice")  # asked for an answer, not an approval
REJECTED = ("rejected", "set_aside", "skipped", "left")


@dataclass
class Tally:
    proposed: int = 0
    approved: int = 0
    edited: int = 0
    rejected: int = 0
    auto: int = 0
    taken_back: int = 0  # applied on their own, then undone by you (also counted as rejected)
    approved_days: set[date] = field(default_factory=set)  # the days those approvals as written were made

    def __bool__(self) -> bool:
        return any((self.proposed, self.approved, self.edited, self.rejected, self.auto))


def kind_label(kind: str) -> str:
    return ROUTE_LABEL.get(kind, kind.replace("_", " ").capitalize())


def _day(stamp: str) -> date:
    return date.fromisoformat(stamp[:10])


class TrustReport:
    def __init__(self, agent: Any):
        self.agent = agent

    def _outcome(self, row: Any, proposal: dict[str, Any], undone: set[str]) -> str | None:
        """How this proposal was settled, or None while it is open. An approval you took back with Undo is a no."""
        if row["status"] in REJECTED:
            return "rejected"
        if row["status"] != "committed":
            return None
        if row["id"] in undone:
            return "rejected"
        if proposal.get("trusted"):
            return "auto"
        return "edited" if proposal.get("edited") or proposal.get("teacher_history") else "approved"

    def tally(self, start: date, end: date) -> dict[str, Tally]:
        """Per kind, what was proposed and what was decided between these days, both ends included."""
        tallies: dict[str, Tally] = {}
        undone = self.agent.ledger.undone_proposals()
        for row in self.agent.ledger.proposals_since(start.isoformat()):
            proposal = json.loads(row["proposal_json"])
            kind = proposal.get("first_kind") or proposal.get("kind")
            if not kind or kind in QUESTIONS:
                continue
            tally = tallies.setdefault(kind, Tally())
            if start <= _day(row["created_at"]) <= end:
                tally.proposed += 1
            outcome = self._outcome(row, proposal, undone) if start <= _day(row["updated_at"]) <= end else None
            if outcome:
                setattr(tally, outcome, getattr(tally, outcome) + 1)
                tally.taken_back += outcome == "rejected" and row["id"] in undone and bool(proposal.get("trusted"))
                if outcome == "approved":
                    tally.approved_days.add(_day(row["updated_at"]))
        return {kind: tally for kind, tally in tallies.items() if tally}

    def lines(self, monday: date, sunday: date) -> list[str]:
        week = self.tally(monday, sunday)
        month = self.tally(sunday - timedelta(days=LOOKBACK_DAYS - 1), sunday)
        on = set(self.agent.settings.trust_bookkeeping)
        lines = ["- Nothing proposed or decided this week."]
        if week:
            lines = ["This week, per kind: proposed, approved as written, edited first, rejected, and applied on "
                     "their own (auto).",
                     "| Kind | Proposed | Approved | Edited | Rejected | Auto |", "|---|---|---|---|---|---|"]
            for kind in sorted(week, key=kind_label):
                t = week[kind]
                lines.append(f"| {kind_label(kind)} | {t.proposed} | {t.approved} | {t.edited} | {t.rejected} | "
                             f"{t.auto} |")
        for kind in sorted((k for k in month if k in on and month[k].taken_back), key=kind_label):
            t = month[kind]
            lines.append(f"- Take back auto-apply: {kind_label(kind)} (`{kind}`) · you undid {t.taken_back} of "
                         f"{t.auto + t.taken_back} in {LOOKBACK_DAYS} days. Remove it from `bookkeeping` in "
                         "config.toml, then run `run.ps1 restart`.")
        earned = sorted((k for k, t in month.items() if k in TRUSTABLE and k not in on and t.approved >= SUGGEST_AFTER
                         and len(t.approved_days) >= SUGGEST_DAYS and not t.edited and not t.rejected),
                        key=kind_label)
        for kind in earned:
            lines.append(f"- Suggest auto-apply: {kind_label(kind)} (`{kind}`) · {month[kind].approved} approved as "
                         f"written on {len(month[kind].approved_days)} different days in the last {LOOKBACK_DAYS}, "
                         "none edited or rejected")
        if earned:
            lines.append("- To turn one on, add its name to `bookkeeping` under `[trust]` in config.toml, then run "
                         "`run.ps1 restart`. What it applies is logged and can be undone.")
        elif week:
            lines.append(f"- Nothing new has earned auto-apply: it takes {SUGGEST_AFTER} approvals as written on "
                         f"{SUGGEST_DAYS} different days in the last {LOOKBACK_DAYS}, none edited or rejected.")
        return lines
