"""Drafts in the user's voice (phase 2 item 7): replies to answered waits and follow-ups.

The model writes; the user edits and sends. Through Bridge an approved draft is saved into his Proton Drafts, threaded
to the mail it answers; otherwise it is text in APPROVAL. The agent never sends: nothing here, or anywhere, speaks
SMTP. 04_REFERENCE/SYSTEM/EMAIL_VOICE.md, when it exists and is not private, guides every draft.
"""
from __future__ import annotations

import re
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any

from .core import SECRET_RE, Settings
from .markdown import split_frontmatter
from .providers import ProviderError

VOICE_NOTE = "SYSTEM/EMAIL_VOICE.md"
MAX_VOICE_CHARS = 6000


def voice(vault: Any, settings: Settings) -> str:
    """the user's writing rules for drafts, or '' when there are none (or the note is private)."""
    note = vault.notes.get(f"{settings.reference_dir}/{VOICE_NOTE}")
    if note is None or str(note.frontmatter.get("private", "")).casefold() == "true" or "#private" in note.text:
        return ""
    _, start = split_frontmatter(note.text)
    return "\n".join(note.text.split("\n")[start:]).strip()[:MAX_VOICE_CHARS]


def address(name: str, email: str) -> str:
    return formataddr((name, email)) if email else ""


def write_draft(agent: Any, vault: Any, kind: str, context: dict[str, Any]) -> dict[str, str] | None:
    """The model's draft, checked, or None when no model may run (or its answer does not hold)."""
    settings = agent.settings
    if not settings.remote_inference or not agent._provider_ready() or not agent.budget.allows("draft"):
        return None
    try:
        value = agent.provider.draft(kind, context, voice(vault, settings))
    except (ProviderError, ValueError, RuntimeError):
        return None
    if not isinstance(value, dict):
        return None
    subject = re.sub(r"\s+", " ", str(value.get("subject") or "")).strip()[:200]
    body = str(value.get("body") or "").replace("\r\n", "\n").strip()[:6000]
    if not subject or not body or SECRET_RE.search(subject + "\n" + body):
        return None
    return {"subject": subject, "body": body}


def draft_op(draft: dict[str, str], to: str, in_reply_to: str = "") -> dict[str, Any]:
    """An op that saves the draft into Proton Drafts when the proposal is approved (Bridge only)."""
    return {"op": "mail_draft", "path": "mail", "to": to, "subject": draft["subject"], "body": draft["body"],
            "in_reply_to": in_reply_to}


def draft_message(settings: Settings, op: dict[str, Any]) -> bytes:
    sender = settings.mail_own_addresses[0] if settings.mail_own_addresses else settings.mail_user
    message = EmailMessage()
    message["From"] = sender
    if op.get("to"):
        message["To"] = op["to"]
    message["Subject"] = op["subject"]
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or "localhost")
    if op.get("in_reply_to"):
        message["In-Reply-To"] = op["in_reply_to"]
        message["References"] = op["in_reply_to"]
    message.set_content(op["body"])
    return message.as_bytes(policy=policy.SMTP)


def shown(body: str) -> list[str]:
    """A draft's lines, safe inside a fenced block in APPROVAL."""
    clean = body.replace("```", "'''").replace("<!--", "").replace("-->", "")
    return clean.split("\n")
