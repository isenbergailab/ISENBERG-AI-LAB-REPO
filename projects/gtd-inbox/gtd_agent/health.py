"""The agent's health: errors go to state/logs/agent.log with their traceback, one plain line is shown, and
the ledger keeps the last run and the last error so TODAY can say whether the agent is well."""
from __future__ import annotations

import json
import logging
import logging.handlers
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

LOGGER_NAME = "gtd_agent"
LOG_BYTES = 1_000_000
LOG_BACKUPS = 5
REPEAT_LOG_SECONDS = 600  # an error that keeps happening is logged in full once, then briefly every 10 minutes
LAST_RUN = "health.last_run"
LAST_ERROR = "health.last_error"


def log_path(state_dir: Path) -> Path:
    return state_dir / "logs" / "agent.log"


def agent_logger(state_dir: Path) -> logging.Logger:
    """The rotating agent log (1 MB x 5). One file per process; a different state folder replaces the old one."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    target = str(log_path(state_dir).resolve())
    for handler in list(logger.handlers):
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            if handler.baseFilename == target:
                return logger
            logger.removeHandler(handler)
            handler.close()
    log_path(state_dir).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(target, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS,
                                                   encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    return logger


def plain_error(exc: BaseException) -> str:
    """One readable line. Errors the agent raises on purpose already speak plainly; others keep their type."""
    detail = " ".join(str(exc).split()) or "no detail"
    if isinstance(exc, PermissionError):
        return f"a file was locked or not allowed ({getattr(exc, 'filename', None) or detail})"
    if isinstance(exc, FileNotFoundError):
        return f"a file was missing ({getattr(exc, 'filename', None) or detail})"
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return f"network problem ({detail})"
    if type(exc).__name__ in {"ProviderError", "TelegramError", "ApprovalError", "OpError", "RuntimeError"}:
        return detail
    return f"{type(exc).__name__}: {detail}"


def _say(message: str) -> None:
    try:
        print(message, flush=True)
    except (OSError, ValueError):  # no console (pythonw) or a closed one
        pass


class Health:
    def __init__(self, ledger: Any, state_dir: Path, out: Callable[[str], None] | None = None) -> None:
        self.ledger = ledger
        self.logger = agent_logger(state_dir)
        self.out = out or _say
        self._failing: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ reporting
    def error(self, label: str, exc: BaseException, at: datetime | None = None) -> None:
        """Log the traceback (once per distinct error), show one line, and keep it as the last error."""
        at = at or datetime.now()
        text = plain_error(exc)
        signature = f"{type(exc).__name__}: {exc}"
        state = self._failing.get(label)
        if state is None or state["signature"] != signature:
            self._failing[label] = {"signature": signature, "count": 1, "logged": time.monotonic()}
            self._log(logging.ERROR, f"{label} failed: {text}", exc)
            self._show(f"CHECK: {label}: {text}. Still running; it tries again on its own.")
            self._write(LAST_ERROR, {"label": label, "text": text, "at": at.isoformat(timespec="seconds"),
                                     "count": 1, "recovered_at": None})
            return
        state["count"] += 1
        if time.monotonic() - state["logged"] >= REPEAT_LOG_SECONDS:
            state["logged"] = time.monotonic()
            self._log(logging.WARNING, f"{label} still failing ({state['count']} times): {text}")
            last = self._read(LAST_ERROR)
            if isinstance(last, dict) and last.get("label") == label:
                last.update(count=state["count"], at=at.isoformat(timespec="seconds"))
                self._write(LAST_ERROR, last)

    def recovered(self, label: str, at: datetime | None = None) -> None:
        state = self._failing.pop(label, None)
        if state is None:
            return
        at = at or datetime.now()
        self._log(logging.INFO, f"{label} recovered after {state['count']} failed attempt(s)")
        self._show(f"RECOVERED: {label}")
        last = self._read(LAST_ERROR)
        if isinstance(last, dict) and last.get("label") == label and not last.get("recovered_at"):
            last["recovered_at"] = at.isoformat(timespec="seconds")
            self._write(LAST_ERROR, last)

    def ran(self, at: datetime | None = None) -> None:
        """A scan cycle finished (whatever its errors): TODAY's report shows this time."""
        try:
            self.ledger.kv_set(LAST_RUN, (at or datetime.now()).isoformat(timespec="seconds"))
        except Exception as exc:  # noqa: BLE001 - health must never break the loop
            self._log(logging.ERROR, "Could not record the last run in the ledger", exc)

    def note(self, message: str) -> None:
        """A message from an operation: shown with the time and kept in the log."""
        self._log(logging.WARNING if message.startswith("CHECK") else logging.INFO, message)
        self._show(message)

    def info(self, message: str) -> None:
        self._log(logging.INFO, message)

    def trace(self, label: str, exc: BaseException) -> None:
        """Keep an error's traceback in the log without making it the agent's last error (e.g. one bad file)."""
        self._log(logging.ERROR, f"{label} failed: {plain_error(exc)}", exc)

    def snapshot(self) -> dict[str, Any]:
        last_error = self._read(LAST_ERROR)
        return {"last_run": self._raw(LAST_RUN), "last_error": last_error if isinstance(last_error, dict) else None}

    # ------------------------------------------------------------ plumbing
    def _show(self, message: str) -> None:
        try:
            self.out(f"{datetime.now():%H:%M} {message}")
        except Exception:  # noqa: BLE001
            pass

    def _log(self, level: int, message: str, exc: BaseException | None = None) -> None:
        try:
            if exc is not None:
                self.logger.log(level, message, exc_info=(type(exc), exc, exc.__traceback__))
            else:
                self.logger.log(level, message)
        except Exception:  # noqa: BLE001 - logging trouble must not stop the agent
            pass

    def _raw(self, key: str) -> str | None:
        try:
            return self.ledger.kv_get(key)
        except Exception:  # noqa: BLE001
            return None

    def _read(self, key: str) -> Any:
        raw = self._raw(key)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def _write(self, key: str, value: Any) -> None:
        try:
            self.ledger.kv_set(key, json.dumps(value, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            self._log(logging.ERROR, "Could not record agent health in the ledger", exc)
