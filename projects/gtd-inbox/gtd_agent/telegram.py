"""Optional Telegram. Text you send the bot from your own chat becomes Inbox lines, and the agent can say things
to that chat on its own (`send`; the pings in pings.py use it).

Only the chat ID in config.toml is accepted. Captures still go through approval.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .credentials import get_secret

API = "https://api.telegram.org/bot{token}/{method}"
HELP = ("Send one thing per line. Each line lands in your GTD Inbox. "
        "Vault syntax works too: #computer, #calls, #anywhere, #errands, #waiting Person: what, #someday. "
        "A caption on a photo or file is captured; the photo or file itself is not saved.")
MAX_TEXT = 4096  # what Telegram takes in one message
NOT_TEXT = "Only text is captured. A caption on a photo or file is captured too."
EDIT_NOTE = "An edit does not change the Inbox. Fix the line there, or send it again."
UPDATES = ["message", "edited_message"]
HELLO = "GTD agent: test message. Pings will reach this chat."


class TelegramError(RuntimeError):
    pass


def call(token: str, method: str, params: dict[str, Any] | None = None, timeout: int = 15) -> Any:
    data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
                                   for k, v in (params or {}).items()}).encode()
    request = urllib.request.Request(API.format(token=token, method=method), data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise TelegramError(f"Telegram request failed ({type(exc).__name__})") from exc
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise TelegramError(f"Telegram refused {method}: {payload.get('description', 'unknown error') if isinstance(payload, dict) else ''}")
    return payload.get("result")


class TelegramCapture:
    def __init__(self, agent: Any):
        self.agent = agent
        self.settings = agent.settings
        self.ledger = agent.ledger

    def token(self) -> str | None:
        return get_secret("telegram", self.settings.state_dir)

    def ready(self) -> bool:
        """Telegram is switched on, and there is a chat to write to and a bot token."""
        return bool(self.settings.telegram_enabled and self.settings.telegram_chat_id and self.token())

    def send(self, text: str) -> None:
        """Say something to your chat, on the agent's own account. Raises TelegramError when it cannot be said."""
        token = self.token()
        if not token:
            raise TelegramError("no Telegram bot token is saved")
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT - 1] + "…"
        call(token, "sendMessage", {"chat_id": self.settings.telegram_chat_id, "text": text,
                                    "disable_web_page_preview": True})

    def poll(self) -> list[str]:
        if not self.settings.telegram_enabled:
            return []
        token = self.token()
        if not token:
            return ["CHECK: Telegram is enabled but no bot token is saved. Run: run.ps1 set-secret telegram"]
        if not self.settings.telegram_chat_id:
            return ["CHECK: Telegram is enabled but [telegram] chat_id is 0. Run: run.ps1 telegram setup"]
        offset = int(self.ledger.kv_get("telegram_offset", "0") or 0)
        updates = call(token, "getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": UPDATES})
        lines: list[str] = []
        replies: list[tuple[int, int, str]] = []
        messages: list[str] = []
        next_offset = offset
        limit = self.settings.max_capture_chars
        for update in updates or []:
            next_offset = max(next_offset, int(update.get("update_id", 0)) + 1)
            edited = update.get("edited_message")
            message = update.get("message") or edited or {}
            chat = (message.get("chat") or {}).get("id")
            if chat != self.settings.telegram_chat_id:
                notice = self._stranger(chat)
                if notice:
                    messages.append(notice)
                continue
            message_id = int(message.get("message_id", 0))
            if edited is not None:
                replies.append((chat, message_id, EDIT_NOTE))
                continue
            text = message.get("text")
            caption = message.get("caption")
            if text and text.strip().startswith(("/start", "/help")):
                replies.append((chat, message_id, HELP))
                continue
            if not (text or caption):
                replies.append((chat, message_id, NOT_TEXT))
                continue
            parts = [part.strip() for part in (text or caption).splitlines() if part.strip()]
            kept = [part for part in parts if len(part) <= limit]  # the Inbox skips a longer line without a word
            lines.extend(kept)
            replies.append((chat, message_id, self._captured(len(kept), len(parts) - len(kept), limit,
                                                             not text)))
        if lines:
            self.agent.append_to_inbox(lines)
            messages.append(f"Telegram: captured {len(lines)} Inbox line{'s' if len(lines) != 1 else ''}")
        if next_offset != offset:
            self.ledger.kv_set("telegram_offset", str(next_offset))
        for chat, message_id, text in replies:
            try:
                call(token, "sendMessage", {"chat_id": chat, "text": text, "reply_to_message_id": message_id})
            except TelegramError:
                pass
        return messages

    @staticmethod
    def _captured(kept: int, too_long: int, limit: int, caption_only: bool) -> str:
        """What the bot answers after a capture: how much landed in the Inbox, and what could not."""
        def lines(count: int) -> str:
            return f"{count} line{'s' if count != 1 else ''}"
        advice = f"over {limit} characters"
        if not kept and too_long:
            return (f"Nothing captured. {lines(too_long)} {'is' if too_long == 1 else 'are'} {advice}: "
                    f"send {'it' if too_long == 1 else 'them'} in shorter pieces.")
        reply = f"Captured {lines(kept)} to your Inbox."
        if caption_only:
            reply += " The photo or file is not saved, only its caption."
        if too_long:
            reply += (f" {lines(too_long)} {'is' if too_long == 1 else 'are'} {advice} and "
                      f"{'was' if too_long == 1 else 'were'} not captured: "
                      f"send {'it' if too_long == 1 else 'them'} in shorter pieces.")
        return reply

    def _stranger(self, chat: Any) -> str | None:
        """A message from a chat that is not yours is never captured or answered. It is reported once a day per
        chat, so a stranger who keeps writing does not fill the log."""
        today = self.agent.today().isoformat()
        for key, day in self.ledger.kv_items("telegram_stranger|").items():
            if day != today:
                self.ledger.kv_delete(key)
        key = f"telegram_stranger|{chat}"
        if self.ledger.kv_get(key) == today:
            return None
        self.ledger.kv_set(key, today)
        return (f"CHECK: ignored a Telegram message from chat {chat}. "
                "If that is you, set [telegram] chat_id to that number.")

    def hello(self) -> list[str]:
        """Say one thing to your chat, so you can see that pings arrive. What happened, as lines to print."""
        if not self.token():
            return ["No bot token saved. Run: run.ps1 set-secret telegram"]
        if not self.settings.telegram_chat_id:
            return ["No chat id yet. Run: run.ps1 telegram setup, then put the number under [telegram] chat_id."]
        if not self.settings.telegram_enabled:
            return ["Telegram is off. Set enabled = true under [telegram] in config.toml."]
        try:
            self.send(HELLO)
        except TelegramError as exc:
            return [f"CHECK: {exc}"]
        return ["Sent. Look for it on your phone."]

    def setup(self) -> list[str]:
        token = self.token()
        if not token:
            return ["No bot token saved. Create a bot with @BotFather, then run: run.ps1 set-secret telegram"]
        me = call(token, "getMe")
        out = [f"Bot: @{me.get('username')} works."]
        updates = call(token, "getUpdates", {"timeout": 0, "allowed_updates": ["message"]}) or []
        chats = {}
        for update in updates:
            chat = (update.get("message") or {}).get("chat") or {}
            if chat.get("id") is not None:
                chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
        if not chats:
            out.append("No messages yet. Send your bot any message from your phone, then run this again.")
        for chat_id, name in chats.items():
            out.append(f"Chat {chat_id} ({name}). Put this in config.toml under [telegram]: chat_id = {chat_id}")
        return out


class RelayCapture:
    """Read immutable Telegram captures delivered by a one-way Syncthing folder."""

    def __init__(self, agent: Any):
        self.agent = agent
        self.settings = agent.settings
        self.ledger = agent.ledger
        self._seen: set[str] = set()

    def _check_once(self, path: Path, reason: str) -> str | None:
        key = f"telegram_relay_bad|{path.name}"
        try:
            stamp = f"{path.stat().st_mtime_ns}|{reason}"
        except OSError:
            stamp = reason
        if self.ledger.kv_get(key) == stamp:
            return None
        self.ledger.kv_set(key, stamp)
        return f"CHECK: Telegram relay skipped {path.name}: {reason}."

    def poll(self) -> list[str]:
        if not self.settings.telegram_enabled:
            return []
        folder = self.settings.telegram_relay_folder
        if folder is None or not folder.is_dir():
            return ["CHECK: Telegram relay folder is missing. Check Syncthing on the vault computer."]
        if not self.settings.telegram_chat_id:
            return ["CHECK: Telegram relay needs [telegram] chat_id."]
        messages: list[str] = []
        def order(path: Path) -> tuple[int, int | str]:
            stem = path.stem
            return (0, int(stem)) if stem.isdecimal() and len(stem) <= 20 else (1, path.name)

        for path in sorted(folder.glob("*.json"), key=order):
            if not path.stem.isdecimal() or len(path.stem) > 20:
                note = self._check_once(path, "invalid file name")
                if note:
                    messages.append(note)
                continue
            key = f"telegram_relay|{path.stem}"
            if key in self._seen or self.ledger.kv_get(key):
                self._seen.add(key)
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    raise ValueError("not a regular file")
                if path.stat().st_size > 16_384:
                    raise ValueError("file too large")
                row = json.loads(path.read_text(encoding="utf-8"))
                if (not isinstance(row, dict) or type(row.get("version")) is not int or row["version"] != 1
                        or type(row.get("update_id")) is not int or str(row["update_id"]) != path.stem
                        or type(row.get("chat_id")) is not int
                        or row["chat_id"] != self.settings.telegram_chat_id
                        or not isinstance(row.get("text"), str) or len(row["text"]) > MAX_TEXT):
                    raise ValueError("invalid capture")
            except (OSError, UnicodeError, ValueError) as exc:
                note = self._check_once(path, str(exc))
                if note:
                    messages.append(note)
                continue
            parts = [part.strip() for part in row["text"].splitlines() if part.strip()]
            kept = [part for part in parts if len(part) <= self.settings.max_capture_chars]
            if kept:
                if self.agent.append_relay_to_inbox(row["update_id"], kept):
                    messages.append(f"Telegram: captured {len(kept)} Inbox line{'s' if len(kept) != 1 else ''}")
            if len(kept) != len(parts):
                messages.append("CHECK: Telegram relay skipped an overlong Inbox line.")
            self.ledger.kv_set(key, "done")
            self._seen.add(key)
            if kept:
                try:
                    self.agent.clear_relay_receipt(row["update_id"])
                except (OSError, RuntimeError):
                    pass  # receipt is invisible; the ledger already prevents another append
        return messages
