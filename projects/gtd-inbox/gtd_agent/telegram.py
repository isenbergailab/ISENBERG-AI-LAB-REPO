"""Optional Telegram capture. Text you send the bot from your own chat becomes Inbox lines.

Only the chat ID in config.toml is accepted. Captures still go through approval.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .credentials import get_secret

API = "https://api.telegram.org/bot{token}/{method}"
HELP = ("Send one thing per line. Each line lands in your GTD Inbox. "
        "Vault syntax works too: #computer, #calls, #anywhere, #errands, #waiting Person: what, #someday.")


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

    def poll(self) -> list[str]:
        if not self.settings.telegram_enabled:
            return []
        token = self.token()
        if not token:
            return ["CHECK: Telegram is enabled but no bot token is saved. Run: run.ps1 set-secret telegram"]
        if not self.settings.telegram_chat_id:
            return ["CHECK: Telegram is enabled but [telegram] chat_id is 0. Run: run.ps1 telegram setup"]
        offset = int(self.ledger.kv_get("telegram_offset", "0") or 0)
        updates = call(token, "getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message"]})
        lines: list[str] = []
        replies: list[tuple[int, int, str]] = []
        messages: list[str] = []
        next_offset = offset
        for update in updates or []:
            next_offset = max(next_offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or {}
            chat = (message.get("chat") or {}).get("id")
            if chat != self.settings.telegram_chat_id:
                messages.append(f"CHECK: ignored a Telegram message from chat {chat}. "
                                "If that is you, set [telegram] chat_id to that number.")
                continue
            text = message.get("text")
            message_id = int(message.get("message_id", 0))
            if not text:
                replies.append((chat, message_id, "Only text is captured."))
                continue
            if text.strip().startswith(("/start", "/help")):
                replies.append((chat, message_id, HELP))
                continue
            parts = [part.strip() for part in text.splitlines() if part.strip()]
            lines.extend(parts)
            replies.append((chat, message_id, f"Captured {len(parts)} line{'s' if len(parts) != 1 else ''} to your Inbox."))
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
