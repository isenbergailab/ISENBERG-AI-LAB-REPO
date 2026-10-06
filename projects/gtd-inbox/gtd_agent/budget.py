"""The monthly model budget. Every call's cost goes in the ledger; at the cap only Inbox lines still reach a model.

Costs come from OpenRouter's usage accounting (`usage.cost`, in USD credits) on every reply. At the warning share
TODAY says how much is used; at the cap every model job pauses until the next month, except the jobs that file
Inbox lines (routing, interpretation, splitting), which cost fractions of a cent.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

INBOX_LINE_JOBS = frozenset({"route", "interpret", "split"})


def _first_of_next_month(today: date) -> date:
    return date(today.year + (today.month == 12), today.month % 12 + 1, 1)


class Budget:
    def __init__(self, ledger: Any, cap_usd: float, warn_percent: int,
                 clock: Callable[[], datetime] = datetime.now) -> None:
        self.ledger = ledger
        self.cap = float(cap_usd)
        self.warn_percent = int(warn_percent)
        self.clock = clock

    def month(self) -> str:
        return self.clock().strftime("%Y-%m")

    def spent(self, month: str | None = None) -> float:
        return self.ledger.model_spend(month or self.month())

    def record(self, job: str, model: str, usage: Any) -> None:
        usage = usage if isinstance(usage, dict) else {}
        cost = usage.get("cost")
        known = isinstance(cost, (int, float)) and not isinstance(cost, bool)
        tokens_in = usage.get("prompt_tokens", usage.get("input_tokens"))
        tokens_out = usage.get("completion_tokens", usage.get("output_tokens"))
        self.ledger.record_model_call(self.month(), job, model, float(cost) if known else 0.0, known,
                                      tokens_in if isinstance(tokens_in, int) else None,
                                      tokens_out if isinstance(tokens_out, int) else None)

    def allows(self, job: str) -> bool:
        return self.cap <= 0 or job in INBOX_LINE_JOBS or self.spent() < self.cap

    def paused_message(self) -> str:
        resume = _first_of_next_month(self.clock().date())
        return (f"Monthly model budget reached (${self.spent():.2f} of ${self.cap:.2f}). Paused until "
                f"{resume:%b} 1 or a higher [budget] monthly_usd; Inbox lines still run")

    def report_line(self) -> str | None:
        """TODAY's budget line: shown from the warning share on, None below it."""
        if self.cap <= 0:
            return None
        spent = self.spent()
        unknown = self.ledger.unknown_cost_calls(self.month())
        note = f" {unknown} call{'s' if unknown != 1 else ''} reported no cost." if unknown else ""
        if spent >= self.cap:
            resume = _first_of_next_month(self.clock().date())
            return (f"Model budget reached: ${spent:.2f} of ${self.cap:.2f} this month. Until {resume:%b} 1 only "
                    f"Inbox lines use a model.{note}")
        percent = spent / self.cap * 100
        if percent >= self.warn_percent:
            return f"Model budget: ${spent:.2f} of ${self.cap:.2f} used this month ({percent:.0f}%).{note}"
        return None
