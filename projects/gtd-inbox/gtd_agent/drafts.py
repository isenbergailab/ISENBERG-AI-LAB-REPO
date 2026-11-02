"""Drafts in the user's voice (phase 2 item 7): replies to answered waits and follow-ups.

The model writes; the user edits and sends. Through Bridge an approved draft is saved into his Proton Drafts, threaded
to the mail it answers; otherwise it is text in APPROVAL. The agent never sends: nothing here, or anywhere, speaks
SMTP. 04_REFERENCE/SYSTEM/EMAIL_VOICE.md, when it exists and is not private, guides every draft.

An approved draft is never lost: it waits in the Outbox (the ledger) until Bridge has taken it, and it is saved once.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any

from .core import SECRET_RE, Settings, process_lock
from .mailbox import BridgeClosed, Mailbox, MailError, MailRefused
from .markdown import split_frontmatter
from .providers import ProviderError

VOICE_NOTE = "SYSTEM/EMAIL_VOICE.md"
MAX_VOICE_CHARS = 6000
OUTBOX = "draft_outbox|"  # draft_outbox|<order>|<proposal id>|<n>: an approved draft waiting for Proton Drafts
SAVED = "draft_saved|"    # draft_saved|<proposal id>|<n>: when that draft reached Proton Drafts
ORDER = "draft_outbox_order"
PROBLEM = "draft_outbox_problem"


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


def draft_ops(proposal: dict[str, Any]) -> list[dict[str, Any]]:
    """The draft ops a proposal carries, in order."""
    return [op for op in proposal.get("ops") or [] if op.get("op") == "mail_draft"]


class Outbox:
    """Approved drafts wait here, in the ledger, until Bridge has taken them into Proton Drafts.

    Bridge closed, a connection that drops, a refusal: the draft stays queued and the next try saves it. Each draft is
    saved once; a marker says it arrived. Nothing here sends."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.ledger = agent.ledger
        self.settings: Settings = agent.settings

    # ------------------------------------------------------------ the queue
    def add(self, ops: list[dict[str, Any]], origin_id: str) -> int:
        """Keep each approved draft until Proton has it."""
        return sum(self.put(origin_id, number, op) for number, op in enumerate(ops))

    def put(self, origin_id: str, number: int, op: dict[str, Any]) -> int:
        tail = f"|{origin_id}|{number}"
        if any(key.endswith(tail) for key, _ in self.waiting()):
            return 0  # already waiting
        order = int(self.ledger.kv_get(ORDER) or 0) + 1
        self.ledger.kv_set(ORDER, str(order))
        entry = {"to": op.get("to", ""), "subject": op["subject"], "body": op["body"],
                 "in_reply_to": op.get("in_reply_to", ""), "queued_at": datetime.now().isoformat(timespec="seconds")}
        self.ledger.kv_set(f"{OUTBOX}{order:08d}{tail}", json.dumps(entry, ensure_ascii=False))
        return 1

    def waiting(self) -> list[tuple[str, dict[str, Any]]]:
        """The drafts waiting for Proton, oldest first."""
        waiting = []
        for key, value in sorted(self.ledger.kv_items(OUTBOX).items()):
            try:
                waiting.append((key, json.loads(value)))
            except ValueError:
                continue
        return waiting

    # ------------------------------------------------------------ saving
    def flush(self) -> list[str]:
        """Save the waiting drafts into Proton Drafts, oldest first, once each. A refused draft stays and the rest
        go on; a closed or dropped Bridge leaves them all. Returns what happened, for the run's output."""
        if not self.waiting():
            self.ledger.kv_delete(PROBLEM)
            return []
        try:
            with process_lock(self.settings.state_dir, "outbox.lock"):
                return self._flush()
        except RuntimeError:
            return []  # another copy of the agent is saving them right now

    def _flush(self) -> list[str]:
        waiting = self.waiting()  # read again inside the lock
        s = self.settings
        if not waiting:
            return []
        if not (s.mail_enabled and s.mail_source == "bridge"):
            self._problem("off", "Mail through Bridge is off in config.toml, so these drafts can't be saved")
            return []
        saved = 0
        problem: tuple[str, str] | None = None
        try:
            with Mailbox(s, read_only=True) as box:
                for key, entry in waiting:
                    marker = SAVED + key.split("|", 2)[2]
                    if self.ledger.kv_get(marker) is None:  # a marker means it arrived and only the cleanup was missed
                        try:
                            box.save_draft(draft_message(s, entry))
                        except MailRefused as exc:
                            problem = ("refused", str(exc))
                            continue
                        self.ledger.kv_set(marker, datetime.now().isoformat(timespec="seconds"))
                    self.ledger.kv_delete(key)
                    saved += 1
        except BridgeClosed as exc:
            problem = ("closed", str(exc))
        except MailError as exc:  # a login Bridge refused, a connection that dropped, no password saved
            problem = ("mail", str(exc))
        lines = []
        if saved:
            lines.append(f"Saved {saved} draft{'s' if saved != 1 else ''} to your Proton Drafts")
            self.agent.log(lines[0])
        if problem is None:
            self.ledger.kv_delete(PROBLEM)
        else:
            self._problem(*problem)
        return lines

    def _problem(self, kind: str, text: str) -> None:
        """Remember why drafts wait (TODAY shows it) and tell LOG once per problem, not once per try."""
        record = json.dumps({"kind": kind, "text": text}, ensure_ascii=False)
        if self.ledger.kv_get(PROBLEM) == record:
            return
        self.ledger.kv_set(PROBLEM, record)
        if kind == "refused":
            self.agent.log(f"CHECK · Proton wouldn't take a draft ({text}); it stays queued and is tried again")
        elif kind == "off":
            self.agent.log(f"CHECK · {text}")
        else:  # closed, or any other reason Bridge could not take it now
            self.agent.log(f"A draft is waiting for Bridge ({text})")

    def today_line(self) -> str:
        """TODAY's line about drafts that have not reached Proton, or '' when none wait."""
        count = len(self.waiting())
        if not count:
            return ""
        line = f"- Drafts waiting for Proton: {count}"
        try:
            problem = json.loads(self.ledger.kv_get(PROBLEM) or "")
        except ValueError:
            return line + ". They save on the next check"
        kind, text = problem.get("kind"), str(problem.get("text", "")).rstrip(".")
        if kind == "closed":
            return f"{line}. They save as soon as Bridge is open"
        if kind == "refused":
            return f"{line}. {text}. They are tried again at every check"
        return f"{line}. {text}"  # off, or another reason Bridge could not take them

    # ------------------------------------------------------------ drafts approved before the Outbox kept them
    def unrecorded(self) -> list[tuple[Any, list[tuple[int, dict[str, Any]]]]]:
        """Approved proposals whose drafts have no record of reaching Proton Drafts and are not waiting."""
        waiting = {key.split("|", 2)[2] for key, _ in self.waiting()}
        found = []
        for row in self.ledger.list("committed"):
            ops = draft_ops(json.loads(row["proposal_json"]))
            missing = [(number, op) for number, op in enumerate(ops)
                       if f"{row['id']}|{number}" not in waiting
                       and self.ledger.kv_get(f"{SAVED}{row['id']}|{number}") is None]
            if missing:
                found.append((row, missing))
        return found

    def resend(self, row: Any) -> int:
        """Queue every draft of this approved proposal again, as another copy for Proton Drafts."""
        count = 0
        for number, op in enumerate(draft_ops(json.loads(row["proposal_json"]))):
            self.ledger.kv_delete(f"{SAVED}{row['id']}|{number}")
            count += self.put(row["id"], number, op)
        return count
