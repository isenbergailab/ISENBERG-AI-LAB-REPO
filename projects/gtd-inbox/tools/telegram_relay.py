"""relay host Telegram capture relay. Standard library only; never opens the vault.

Run with ``python telegram_relay.py --env /private/path/telegram.env``. The env file
must stay outside the Syncthing folder. Only JSON captures enter that folder.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


API = "https://api.telegram.org/bot{token}/{method}"
UPDATES = ["message", "edited_message"]
QUEUED = "Queued for your GTD Inbox. It will arrive when your vault computer syncs."
EDITED = "An edit does not change the Inbox. Fix the line there, or send it again."
NOT_TEXT = "Only text is captured. A caption on a photo or file is captured too."
HELP = "Send one thing per line. Text and captions reach your GTD Inbox after Syncthing syncs."


class RelayError(RuntimeError):
    pass


def telegram_call(token: str, method: str, params: dict[str, Any], timeout: int = 45) -> Any:
    data = urllib.parse.urlencode({key: json.dumps(value) if isinstance(value, list) else value
                                   for key, value in params.items()}).encode()
    request = urllib.request.Request(API.format(token=token, method=method), data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, TimeoutError, ValueError) as exc:
        raise RelayError(f"Telegram request failed ({type(exc).__name__})") from exc
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RelayError(f"Telegram refused {method}")
    return payload.get("result")


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Relay:
    def __init__(self, token: str, chat_id: int, spool: Path, state: Path,
                 api: Callable[..., Any] = telegram_call):
        if not token or not chat_id:
            raise ValueError("Relay needs bot token and chat ID")
        self.token = token
        self.chat_id = chat_id
        self.spool = spool
        self.state = state
        self.api = api

    def _offset(self) -> int:
        path = self.state / "offset.txt"
        return int(path.read_text(encoding="ascii").strip()) if path.exists() else 0

    def _queue(self, update_id: int, message: dict[str, Any]) -> None:
        path = self.spool / f"{update_id}.json"
        text = message.get("text") or message.get("caption")
        row = {"version": 1, "update_id": update_id, "chat_id": self.chat_id, "text": text}
        if path.exists():
            if path.is_file() and not path.is_symlink():
                try:
                    if json.loads(path.read_text(encoding="utf-8")) == row:
                        return
                except (OSError, UnicodeError, ValueError):
                    pass
            raise OSError("Existing spool capture is invalid")
        atomic_text(path, json.dumps(row, ensure_ascii=False, separators=(",", ":")))

    def _reply(self, message: dict[str, Any], text: str) -> None:
        try:
            self.api(self.token, "sendMessage", {"chat_id": self.chat_id, "text": text,
                                                  "reply_to_message_id": message.get("message_id")})
        except RelayError:
            pass  # a reply failure cannot undo a durable capture

    def poll_once(self) -> int:
        """Persist each capture before acknowledging its update to Telegram."""
        offset = self._offset()
        updates = self.api(self.token, "getUpdates", {"offset": offset, "timeout": 30,
                                                       "allowed_updates": UPDATES}, timeout=45)
        if not isinstance(updates, list):
            raise RelayError("Telegram returned invalid updates")
        processed = 0
        for row in updates:
            if not isinstance(row, dict) or type(row.get("update_id")) is not int:
                raise RelayError("Telegram returned an invalid update ID")
            update_id = row["update_id"]
            if update_id < offset:
                continue
            edited = row.get("edited_message")
            message = row.get("message") or edited
            if isinstance(message, dict) and isinstance(message.get("chat"), dict) and \
                    message["chat"].get("id") == self.chat_id:
                body = message.get("text") or message.get("caption")
                if edited is not None:
                    self._reply(message, EDITED)
                elif isinstance(body, str) and body.strip().startswith(("/start", "/help")):
                    self._reply(message, HELP)
                elif isinstance(body, str) and body.strip():
                    self._queue(update_id, message)
                    self._reply(message, QUEUED)
                else:
                    self._reply(message, NOT_TEXT)
            offset = update_id + 1
            atomic_text(self.state / "offset.txt", f"{offset}\n")
            processed += 1
        return processed


def load_env(path: Path) -> Relay:
    if not path.is_file():
        raise ValueError("Relay env file is missing")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("Relay env file must be private to your account (chmod 600)")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError("Relay env file has an invalid line")
        values[key.strip()] = value.strip().strip('"').strip("'")
    spool = Path(values.get("GTD_TELEGRAM_SPOOL", "")).expanduser().resolve()
    state = Path(values.get("GTD_TELEGRAM_STATE", "")).expanduser().resolve()
    if not values.get("GTD_TELEGRAM_SPOOL") or not values.get("GTD_TELEGRAM_STATE"):
        raise ValueError("Relay env needs spool and state folders")
    if path.resolve() == spool or spool in path.resolve().parents or state == spool or spool in state.parents:
        raise ValueError("Relay env and state must stay outside Syncthing spool")
    return Relay(values.get("GTD_TELEGRAM_TOKEN", ""), int(values.get("GTD_TELEGRAM_CHAT_ID", "0")),
                 spool, state)


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram capture relay for relay host")
    parser.add_argument("--env", required=True, type=Path, help="private env file, outside Syncthing")
    args = parser.parse_args()
    relay = load_env(args.env)
    while True:
        try:
            relay.poll_once()
        except (OSError, RelayError, ValueError) as exc:
            print(f"CHECK: Telegram relay paused ({type(exc).__name__}).", flush=True)
            time.sleep(15)


if __name__ == "__main__":
    raise SystemExit(main())
