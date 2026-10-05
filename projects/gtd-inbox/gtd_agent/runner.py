"""The agent's run loop: each operation on its interval, and no error ever stops it.

An error goes to the agent's health (traceback in state/logs/agent.log, one printed line, last error in the
ledger for TODAY) and the loop carries on. Only Ctrl+C (KeyboardInterrupt) and SystemExit end it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .core import process_lock
from .health import Health


@dataclass
class Operation:
    label: str
    run: Callable[[], Any]
    every: str = "scan"  # "poll": every poll interval; "scan": every scan interval
    wakes_scan: Callable[[list[str]], bool] | None = None  # messages that call for a scan right away
    again_in: Callable[[], float | None] | None = None  # asked after each run: seconds until it wants to run
    #                                                     again, ahead of the next scan. None: the next scan will do


class Control:
    """Stop and restart requests: files in the state folder that a running agent obeys between cycles.

    A hidden agent has no window to close, so `stop` and `restart` (or anyone with the state folder) leave a
    request file. A request older than this copy of the agent is left over from an earlier run and ignored.
    """

    NAMES = ("stop", "restart")

    def __init__(self, state_dir: Path, started: float | None = None) -> None:
        self.state_dir = state_dir
        self.started = time.time() if started is None else started

    def path(self, name: str) -> Path:
        return self.state_dir / f"{name}.request"

    def request(self, name: str) -> Path:
        path = self.path(name)
        path.write_text(f"{name} requested {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8")
        return path

    def pending(self) -> str | None:
        found = None
        for name in self.NAMES:
            path = self.path(name)
            try:
                made = path.stat().st_mtime
            except OSError:
                continue
            try:
                path.unlink()
            except OSError:
                pass
            if found is None and made >= self.started:
                found = name
        return found


class Runner:
    def __init__(self, health: Health, operations: list[Operation], poll_seconds: float, scan_seconds: float, *,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 control: Control | None = None) -> None:
        self.health = health
        self.operations = operations
        self.poll_seconds = poll_seconds
        self.scan_seconds = scan_seconds
        self.clock = clock
        self.sleep = sleep
        self.control = control
        self.next_scan = 0.0
        self.early: dict[str, float] = {}  # operation label -> when it asked to run again

    def attempt(self, operation: Operation) -> list[str]:
        try:
            result = operation.run()
        except Exception as exc:  # noqa: BLE001 - the loop survives anything except Ctrl+C and SystemExit
            self.health.error(operation.label, exc)
            return []
        self.health.recovered(operation.label)
        messages = [str(message) for message in result] if isinstance(result, (list, tuple)) else []
        for message in messages:
            self.health.note(message)
        return messages

    def cycle(self) -> None:
        scan_due = self.clock() >= self.next_scan
        scanned = False
        for operation in self.operations:
            if operation.every == "scan":
                if not scan_due and self.clock() < self.early.get(operation.label, float("inf")):
                    continue
                if scan_due and not scanned:
                    self.next_scan = self.clock() + self.scan_seconds
                    scanned = True
                self.attempt(operation)
                wait = operation.again_in() if operation.again_in is not None else None
                if wait is None:
                    self.early.pop(operation.label, None)
                else:
                    self.early[operation.label] = self.clock() + wait
                continue
            messages = self.attempt(operation)
            if operation.wakes_scan is not None and messages and operation.wakes_scan(messages):
                scan_due = True
        if scanned:
            self.health.ran()

    def run(self, cycles: int | None = None) -> str:
        """Loop until a stop or restart request (returned as "stop" or "restart"), or for a number of cycles."""
        done = 0
        while cycles is None or done < cycles:
            request = self.control.pending() if self.control is not None else None
            if request is not None:
                return request
            self.cycle()
            done += 1
            if cycles is None or done < cycles:
                self.sleep(self.poll_seconds)
        return "done"


def scan_inbox(agent: Any) -> list[str]:
    result = agent.scan()
    messages = [f"CHECK: {error}" for error in result.errors]
    if result.new_proposals:
        messages.append(f"{len(result.new_proposals)} new proposal(s) in APPROVAL.md")
    return messages


def refresh_approvals(agent: Any) -> list[str]:
    with process_lock(agent.settings.state_dir):
        agent.render_reviews()
    return []


def loop_operations(agent: Any) -> list[Operation]:
    """The agent's work, in loop order: approvals and Telegram every poll; Inbox and upkeep every scan. A scan
    that finds the user still typing in the Inbox comes back for it as soon as the Inbox is quiet."""
    from .housekeeping import Housekeeper
    from .telegram import TelegramCapture

    settings = agent.settings
    housekeeper = Housekeeper(agent)
    operations: list[Operation] = []
    if settings.mode == "approval":
        operations.append(Operation("Approvals", agent.sync_review_requests, "poll"))
    if settings.telegram_enabled:
        telegram = TelegramCapture(agent)
        operations.append(Operation("Telegram", telegram.poll, "poll",
                                    wakes_scan=lambda messages: any(m.startswith("Telegram: captured")
                                                                    for m in messages)))
    operations.append(Operation("Inbox scan", lambda: scan_inbox(agent), "scan",
                                again_in=lambda: agent.inbox_quiet_in))
    operations.append(Operation("Housekeeping", housekeeper.run, "scan"))
    if settings.mode == "approval":
        operations.append(Operation("Approval refresh", lambda: refresh_approvals(agent), "poll"))
    return operations
