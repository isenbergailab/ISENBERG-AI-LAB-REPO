"""Mail intake: Proton mail the user labels GTD, read through Bridge (docs/PRIVACY.md), or the agent's own inbox (docs/PRIVACY.md).

Through Bridge the agent reads only the label folder and changes nothing in the mailbox. In the agent inbox a
message counts only when it comes from one of the user's own addresses and the receiving server's first
Authentication-Results header shows DKIM or DMARC passing for that address's domain. Anything else is judged on
its headers alone: it moves to Ignored and is never read further.

A message that counts is read once: the original (sender, date, subject, body without its quoted history), taken
out of the user's forward when he forwarded it, then the sensitive screen runs before anything is stored or any model
sees it. A hit leaves only a count. Mail the user writes to himself becomes Inbox lines with no model; the rest goes
to the model and becomes one proposal.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import re
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesHeaderParser, BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

from .calendar_ics import Invite, invites as read_invites
from .clarification import (area_candidates, blank_draft, calendar_by_hand, compile_ops, draft_from_interpretation,
                            new_marker, project_candidates, validate_interpretation)
from .core import Settings
from .drafts import address as mail_address, draft_op, write_draft
from .pdf import PdfError, is_pdf, pdf_text
from .mailbox import SENT, MailError, Mailbox
from .providers import ProviderError
from .screen import ORDER, screen
from .tasks import Task, with_dependency, with_id, with_tag, without_dependency

IGNORED = "GTD Ignored"  # this agent's own folders, so other agents can share the mailbox
SKIPPED = "GTD Skipped"
DONE = "GTD Done"
RECIPIENT_HEADERS = ("To", "Cc", "Delivered-To", "X-Original-To", "Envelope-To", "X-Envelope-To")
HEADER_BATCH = 200
SENT_DAYS = 30
SENT_LOOK = 500
MSGID_RE = re.compile(r"<[^<>\s]+>")
MAX_BODY_CHARS = 20_000
MAX_TRIES = 3
RETRY_MINUTES = 60
SEPARATOR_RE = re.compile(r"^\s*-{2,}\s*(?:Forwarded|Original)\s+message\s*-{2,}\s*$", re.IGNORECASE)
APPLE_RE = re.compile(r"^\s*Begin forwarded message:\s*$", re.IGNORECASE)  # Apple Mail
RULE_RE = re.compile(r"^\s*_{5,}\s*$")  # Outlook's line above the forwarded headers
FORWARD_SUBJECT_RE = re.compile(r"^\s*fwd?\s*:", re.IGNORECASE)
FIELD_RE = re.compile(r"^(From|Date|Sent|Subject|To|CC|Cc|Bcc):\s*(.*)$")
WROTE_RE = re.compile(r"^On\s.{0,240}\bwrote:\s*$")
SIGNATURE_RE = re.compile(r"^\s*Sent (?:with|from) Proton Mail\b", re.IGNORECASE)
ZONE_RE = re.compile(r"[+-]\d{4}\b|\bGMT\b|\bUTC\b")
DATE_RE = re.compile(r"(?:[A-Za-z]+,?\s+)?([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})"
                     r"(?:,?\s+(?:at\s+)?(\d{1,2}):(\d{2})\s*([AaPp]\.?[Mm]\.?)?)?")
MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): number for number, name in enumerate(calendar.month_abbr) if name}, sept=9)
MAX_PER_CHECK = 50
MAX_CAPTURE_LINES = 50  # Inbox lines from one mail to yourself, after its subject
CAPTURE_BULLET_RE = re.compile(r"^(?:[-*+\u2022]\s+)?(?:\[[ xX]?\]\s+)?")
DOCTOR_LOOK = 200
COMMENT_RE = re.compile(r"\([^()]*\)")
NOT_YOURS = "not from your addresses"
UNVERIFIED = "from your address, without a passing DKIM or DMARC result"
Result = tuple[str, str, dict[str, str]]


def parse_headers(raw: bytes) -> Message:
    return BytesHeaderParser().parsebytes(raw)


def _uncommented(value: str) -> str:
    text = str(value)
    while COMMENT_RE.search(text):
        text = COMMENT_RE.sub(" ", text)
    return text


def authservs(headers: Message) -> list[str]:
    """Who wrote each Authentication-Results header, top first."""
    names = []
    for value in headers.get_all("Authentication-Results") or []:
        words = _uncommented(value).split(";")[0].split()
        names.append(words[0].lower() if words else "")
    return names


def auth_results(headers: Message, server: str) -> list[Result] | None:
    """The results in the first Authentication-Results header the receiving server wrote (its authserv-id is
    `server` or ends in '.server'), or None when it wrote none. Headers further down may come from the sender,
    so only the first one counts."""
    for value in headers.get_all("Authentication-Results") or []:
        head, *parts = _uncommented(value).split(";")
        words = head.split()
        authserv = words[0].lower() if words else ""
        if authserv != server and not authserv.endswith("." + server):
            continue
        results: list[Result] = []
        for part in parts:
            words = part.split()
            if not words or "=" not in words[0]:
                continue
            method, _, result = words[0].partition("=")
            props = {}
            for word in words[1:]:
                key, eq, found = word.partition("=")
                if eq:
                    props[key.lower()] = found.strip('"').lower()
            results.append((method.split("/")[0].strip().lower(), result.lower(), props))
        return results
    return None


def sender(headers: Message) -> str | None:
    """The one From address, lower case, or None when there isn't exactly one."""
    addresses = [address.lower() for _, address in getaddresses(headers.get_all("From") or []) if address]
    return addresses[0] if len(addresses) == 1 else None


def addressed_to_agent(headers: Message, settings: Settings) -> bool:
    """In a mailbox shared by several agents, this one owns only mail sent to its own address ([mail] address).
    Without one, everything in the inbox is its own."""
    if not settings.mail_address:
        return True
    values = [str(value) for name in RECIPIENT_HEADERS for value in headers.get_all(name) or []]
    return settings.mail_address in {address.lower() for _, address in getaddresses(values) if address}


def _aligned(domain: str, signer: str) -> bool:
    signer = signer.rpartition("@")[2]
    return bool(signer) and (domain == signer or domain.endswith("." + signer))


def sender_problem(headers: Message, settings: Settings) -> str | None:
    """None when the message is the user's own and authenticated; otherwise why it doesn't count."""
    address = sender(headers)
    if address is None or not _own(address, settings):
        return NOT_YOURS
    domain = address.rpartition("@")[2]
    for method, result, props in auth_results(headers, settings.mail_auth_server) or []:
        if result != "pass":
            continue
        if method == "dmarc" and props.get("header.from") == domain:
            return None
        if method == "dkim" and _aligned(domain, props.get("header.d") or props.get("header.i", "")):
            return None
    return UNVERIFIED


def _plural(count: int, word: str, many: str = "") -> str:
    return f"{count} {word if count == 1 else (many or word + 's')}"


def inbox_summary(settings: Settings) -> tuple[str, str | None]:
    """For doctor, from a read-only look at headers only: what waits, and a problem when mail from the user's
    address arrives without results the agent can trust. Bridge: how much carries the label."""
    if settings.mail_source == "bridge":
        with Mailbox(settings, read_only=True) as box:
            count = len(box.uids(settings.mail_folder, readonly=True))
        return f"{count} in {settings.mail_folder}", None
    with Mailbox(settings) as box:
        uids = box.uids("INBOX", readonly=True)
        everything = [parse_headers(raw) for raw in box.headers(uids[:DOCTOR_LOOK]).values()]
    heads = [headers for headers in everything if addressed_to_agent(headers, settings)]
    elsewhere = len(everything) - len(heads)
    verdicts = [sender_problem(headers, settings) for headers in heads]
    yours = sum(verdict is None for verdict in verdicts)
    line = f"{_plural(len(heads), 'message')} waiting"
    if heads:
        line += f": {yours} from you, {_plural(len(heads) - yours, 'other')}"
    if elsewhere:
        line += f" · {elsewhere} for other addresses"
    unverified = [headers for headers, verdict in zip(heads, verdicts) if verdict == UNVERIFIED]
    if not unverified:
        return line, None
    seen = sorted({name for headers in unverified for name in authservs(headers) if name}) or ["none"]
    return line, (f"{_plural(len(unverified), 'message')} from your address {'has' if len(unverified) == 1 else 'have'} "
                  f"no passing DKIM or DMARC result from {settings.mail_auth_server} (results seen from: "
                  f"{', '.join(seen)}). Ask Claude before relying on mail.")


# ---------------------------------------------------------------- reading a forward
@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    data: bytes
    text: str = ""     # a PDF's text, read locally
    problem: str = ""  # why a PDF was not read
    events: tuple[Invite, ...] = ()  # an invitation's events (.ics)


@dataclass(frozen=True)
class Forward:
    """One message to read: the original the user forwarded or labeled (his own mail to others included), or
    (forwarded False) mail he wrote to himself, a capture. A forward someone else sent him is their mail: the
    message they passed on stays in the body, under their words."""
    message_id: str
    forwarded: bool
    note: str                     # what the user wrote above the forwarded part (or to himself), signature cut
    sender_name: str = ""
    sender: str = ""
    date: datetime | None = None  # in the user's zone
    subject: str = ""
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    body: str = ""                # the original's text, quoted history cut
    attachments: tuple[Attachment, ...] = ()
    passed_on: tuple[str, ...] = ()  # addresses in a message the sender passed on: screened like the sender's


class _HtmlText(HTMLParser):
    BREAKS = {"br", "p", "div", "li", "tr", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "table", "ul",
              "ol", "hr"}
    HIDDEN = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.hidden = 0

    def _break(self, force: bool = False) -> None:
        self.text = self.text.rstrip(" ")
        if force or (self.text and not self.text.endswith("\n")):
            self.text += "\n"

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.HIDDEN:
            self.hidden += 1
        elif tag in self.BREAKS:
            self._break(force=tag == "br")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if tag in self.BREAKS:
            self._break(force=tag == "br")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        elif tag in self.BREAKS and tag != "br":
            self._break()

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        text = re.sub(r"\s+", " ", data)
        if not self.text or self.text.endswith("\n"):
            text = text.lstrip()
        self.text += text


def html_to_text(html: str) -> str:
    parser = _HtmlText()
    parser.feed(html)
    parser.close()
    return "\n".join(line.strip() for line in parser.text.split("\n")).strip()


def _text_of(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    return str(content).replace("\r\n", "\n").replace("\r", "\n")


def _attachments(message: EmailMessage) -> tuple[Attachment, ...]:
    found = []
    for part in message.walk():
        kind = part.get_content_type()
        if part.is_multipart() or kind == "message/rfc822":
            continue
        filename = part.get_filename() or ""
        if not filename and part.get_content_disposition() != "attachment" and kind != "text/calendar":
            continue
        data = part.get_payload(decode=True) or b""
        text, problem, events = "", "", ()
        if is_pdf(filename, kind):
            try:
                text = pdf_text(data, MAX_BODY_CHARS)
            except PdfError as exc:
                problem = str(exc)
        elif kind in {"text/calendar", "application/ics"} or filename.lower().endswith(".ics"):
            events = tuple(read_invites(data.decode("utf-8", errors="replace")))
        found.append(Attachment(filename, kind, data, text, problem, events))
    return tuple(found)


def _note(lines: list[str]) -> str:
    kept = []
    for line in lines:
        if SIGNATURE_RE.match(line) or line.rstrip() == "--":
            break
        kept.append(line.rstrip())
    return "\n".join(kept).strip()


def _unquoted(lines: list[str]) -> list[str]:
    """Proton quotes a forwarded original with '> '; take that level off."""
    filled = [line for line in lines if line.strip()]
    if not filled or sum(line.lstrip().startswith(">") for line in filled) < 0.6 * len(filled):
        return lines
    return [re.sub(r"^\s*> ?", "", line) if line.lstrip().startswith(">") else line for line in lines]


def _history_start(lines: list[str]) -> int:
    """The first line of older mail (a quote, an 'On ... wrote:' line, a separator or a bare header block), or
    len(lines) when there is none."""
    for index, line in enumerate(lines):
        stripped = line.strip()
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if (stripped.startswith(">") or SEPARATOR_RE.match(stripped) or WROTE_RE.match(stripped)
                or (stripped.startswith("On ") and WROTE_RE.match(f"{stripped} {following}"))
                or (stripped.startswith("From:") and following.startswith(("Sent:", "Date:")))):
            return index
    return len(lines)


def _without_history(lines: list[str]) -> str:
    """The text above the first sign of older mail."""
    return "\n".join(line.rstrip() for line in lines[:_history_start(lines)]).strip()


def _addresses(value: str) -> tuple[str, ...]:
    return tuple(address.lower() for _, address in getaddresses([value]) if address)


def _bare(address: str) -> str:
    """you+gtd@pm.me is you@pm.me: a +alias lands in the same mailbox."""
    local, at, domain = address.strip().lower().partition("@")
    return f"{local.split('+', 1)[0]}@{domain}" if at else local


def _own(address: str, settings: Settings) -> bool:
    return bool(address) and _bare(address) in {_bare(own) for own in settings.mail_own_addresses}


def _self_addresses(settings: Settings) -> set[str]:
    """Where mail to yourself goes: the user's own addresses (any +alias of them), and in the agent inbox the agent's."""
    return {_bare(a) for a in (*settings.mail_own_addresses, settings.mail_address, settings.mail_user) if a.strip()}


def _forward_start(lines: list[str], sure: bool) -> tuple[int, int] | None:
    """Where a forwarded message starts: (the end of what the sender wrote above it, its first header line). A
    '--- Forwarded message ---' line and Apple Mail's 'Begin forwarded message:' say forward in any mail. Outlook's
    marks (an 'Original Message' line, a line of underscores, a bare From:/Sent: block) also head the history under
    a reply, so they count only when the caller is sure the mail is a forward: the user's own, or its subject says FW."""
    def first_filled(after: int) -> int | None:
        return next((i for i in range(after, len(lines)) if lines[i].strip()), None)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if SEPARATOR_RE.match(stripped) and (sure or "forwarded" in stripped.lower()):
            return index, first_filled(index + 1) or index + 1
        if APPLE_RE.match(stripped) or (sure and RULE_RE.match(stripped)):
            first = first_filled(index + 1)
            if first is not None and lines[first].strip().startswith("From:"):
                return index, first
        elif sure and stripped.startswith("From:"):
            following = first_filled(index + 1)
            if following is not None and lines[following].strip().startswith(("Sent:", "Date:")):
                return index, index
    return None


def _local(moment: datetime, zone: str) -> datetime:
    if moment.tzinfo is None:
        return moment
    try:
        return moment.astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    except Exception:  # no tz database (Windows without tzdata): the computer's own zone
        return moment.astimezone().replace(tzinfo=None)


def forward_date(text: str, zone: str) -> datetime | None:
    """A date as a forward shows it ('On Tuesday, September 29th, 2026 at 10:00 AM', Gmail's 'Tue, Sep 29, 2026 at
    10:00 AM', or a mail header date), in the user's zone."""
    text = re.sub(r"^\s*On\s+", "", text.strip())
    if not text:
        return None
    if ZONE_RE.search(text):
        try:
            return _local(parsedate_to_datetime(text), zone)
        except (TypeError, ValueError, IndexError):
            pass
    match = DATE_RE.search(text)
    month = MONTHS.get(match.group(1).lower()) if match else None
    if match is None or month is None:
        return None
    hour, minute = int(match.group(4) or 0), int(match.group(5) or 0)
    meridiem = (match.group(6) or "").lower().replace(".", "")
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    try:
        return datetime(int(match.group(3)), month, int(match.group(2)), hour, minute)
    except ValueError:
        return None


def read_forward(raw: bytes, settings: Settings) -> Forward:
    """Read one message: the user's own forward as the original inside it plus his note, his mail to himself as a
    capture, and anyone else's mail as it came."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    message_id = str(message.get("Message-ID", "") or "").strip()
    attachments = _attachments(message)
    lines = _text_of(message).split("\n")
    name, address = parseaddr(str(message.get("From", "") or ""))
    address = address.lower()
    own = _own(address, settings)
    subject = str(message.get("Subject", "") or "").strip()
    to, cc = _addresses(str(message.get("To", "") or "")), _addresses(str(message.get("Cc", "") or ""))
    found = _forward_start(lines, own or bool(FORWARD_SUBJECT_RE.match(subject)))
    if found is not None and not own and _history_start(lines[:found[0]]) < found[0]:
        found = None  # the mark sits in old history under a reply: nothing was passed on
    if found is None:
        inner = next((part.get_payload(0) for part in message.walk()
                      if part.get_content_type() == "message/rfc822"), None) if own else None
        if own and inner is None and not attachments and {_bare(a) for a in to + cc} <= _self_addresses(settings):
            return Forward(message_id, False, _note(_without_history(lines).split("\n")), subject=subject)
        if isinstance(inner, EmailMessage):  # forwarded as an attachment
            name, address = parseaddr(str(inner.get("From", "") or ""))
            return Forward(message_id, True, _note(lines), name.strip(), address.lower(),
                           forward_date(str(inner.get("Date", "") or ""), settings.timezone),
                           str(inner.get("Subject", "") or ""), _addresses(str(inner.get("To", "") or "")),
                           _addresses(str(inner.get("Cc", "") or "")), _without_history(_text_of(inner).split("\n")),
                           attachments + _attachments(inner))
        # an original: someone else's mail as it came, or the user's own to others or with attachments
        return Forward(message_id, True, "", name.strip(), address,
                       forward_date(str(message.get("Date", "") or ""), settings.timezone), subject, to, cc,
                       _without_history(lines), attachments)
    start, index = found
    fields: dict[str, str] = {}
    while index < len(lines):
        match = FIELD_RE.match(lines[index].strip())
        if match is None:
            break
        fields.setdefault(match.group(1).lower(), match.group(2).strip())
        index += 1
    if not own:  # someone else passing a message on: their mail, with that message kept under their words
        body = "\n\n".join(part for part in (
            _without_history(lines[:start]), "\n".join(line.strip() for line in lines[start:index] if line.strip()),
            _without_history(_unquoted(lines[index:]))) if part)
        passed_on = tuple(a for key in ("from", "to", "cc") for a in _addresses(fields.get(key, "")))
        return Forward(message_id, True, "", name.strip(), address,
                       forward_date(str(message.get("Date", "") or ""), settings.timezone), subject, to, cc, body,
                       attachments, passed_on)
    name, address = parseaddr(fields.get("from", ""))
    return Forward(message_id, True, _note(lines[:start]), name.strip(), address.lower(),
                   forward_date(fields.get("date") or fields.get("sent") or "", settings.timezone),
                   fields.get("subject", ""), _addresses(fields.get("to", "")), _addresses(fields.get("cc", "")),
                   _without_history(_unquoted(lines[index:])), attachments)


def screen_mail(mail: Forward, settings: Settings) -> str | None:
    """The sensitive screen over everything a model could see of this mail. Mail to yourself is your own capture:
    ExampleCorp, secrets and your lists only."""
    texts = [mail.note, mail.subject, mail.body, mail.sender_name,
             *(part for a in mail.attachments for part in (a.filename, a.text)),
             *(f"{event.summary} {event.location}" for a in mail.attachments for event in a.events)]
    return screen(texts, [mail.sender, *mail.to, *mail.cc, *mail.passed_on], settings, categories=mail.forwarded)


def capture_lines(mail: Forward) -> list[str]:
    """Mail to yourself as Inbox lines: the subject, then each line above the signature, bullets dropped and
    repeats skipped. Past MAX_CAPTURE_LINES a last line says how much is left in the mail."""
    lines: list[str] = []
    seen: set[str] = set()
    for line in [mail.subject, *mail.note.split("\n")]:
        line = CAPTURE_BULLET_RE.sub("", line.strip()).strip()
        if re.search(r"\w", line) and line.casefold() not in seen:
            seen.add(line.casefold())
            lines.append(line)
    limit = MAX_CAPTURE_LINES + (1 if mail.subject.strip() else 0)
    if len(lines) <= limit:
        return lines
    what = f"the mail '{_plain(mail.subject, 80)}'" if mail.subject.strip() else "your mail to yourself"
    return [*lines[:limit], f"Read the rest of {what}: {_plural(len(lines) - limit, 'more line')}"]


def _message_key(headers: Message, raw: bytes) -> str:
    identity = str(headers.get("Message-ID", "") or "").strip().lower()
    return hashlib.sha256(identity.encode("utf-8") if identity else raw).hexdigest()[:24]


def mail_today(ledger: Any, today: date) -> str:
    """TODAY's mail line: counts only, never a sender or subject."""
    stored = json.loads(ledger.kv_get(f"mail_day|{today.isoformat()}") or "{}")
    problem = ledger.kv_get("mail_problem") or ""
    parts = [f"not connected: {problem}"] if problem else []
    if stored.get("captured"):
        parts.append(f"{_plural(stored['captured'], 'message')} to yourself captured")
    if stored.get("read"):
        quiet = f" ({stored['quiet']} with nothing to act on)" if stored.get("quiet") else ""
        parts.append(f"{_plural(stored['read'], 'message')} read{quiet}")
    skipped = stored.get("skipped") or {}
    if skipped:
        names = [name for name in ORDER if name in skipped] + sorted(set(skipped) - set(ORDER))
        parts.append(f"{sum(skipped.values())} skipped as sensitive ({', '.join(names)})")
    if stored.get("ignored"):
        parts.append(f"{stored['ignored']} ignored (not from you)")
    return " · ".join(parts) or "nothing new"


def _listed(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def contact_notes(vault: Any, settings: Settings) -> list[dict[str, Any]]:
    """Each contact note in 04_REFERENCE/CONTACTS: its key, the names it goes by and the addresses the user confirmed."""
    base = f"{settings.reference_dir}/CONTACTS/"
    return [{"rel": rel, "key": note.stem, "names": _listed(note.frontmatter.get("aliases")),
             "emails": [email.lower() for email in _listed(note.frontmatter.get("emails"))]}
            for rel, note in sorted(vault.notes.items())
            if rel.startswith(base) and note.frontmatter.get("type") == "contact"]


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _plain(value: Any, maximum: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).replace("<!--", "").replace("-->", "").strip()
    return text[:maximum]


def _fmt(day: date) -> str:
    return f"{day:%a %b} {day.day}"


class MailIntake:
    """The housekeeping step that reads the agent inbox."""

    def __init__(self, housekeeper: Any) -> None:
        self.hk = housekeeper
        self.agent = housekeeper.agent
        self.settings: Settings = housekeeper.settings
        self.ledger = housekeeper.ledger

    def due(self, now: datetime) -> bool:
        last = self.ledger.kv_get("mail_checked")
        if last:
            try:
                if now < datetime.fromisoformat(last) + timedelta(minutes=self.settings.mail_check_minutes):
                    return False
            except ValueError:
                pass
        self.ledger.kv_set("mail_checked", now.isoformat(timespec="seconds"))
        return True

    def run(self, vault: Any, now: datetime | None = None) -> list[str]:
        if not self.settings.mail_enabled or not self.due(now or datetime.now()):
            return []
        read, quiet, waiting, ignored, captured, skipped = 0, 0, 0, 0, 0, {}
        messages: list[str] = []
        blocked = self._model_problem()
        bridge = self.settings.mail_source == "bridge"  # Proton: read the label, change nothing in the mailbox

        def file(uid: int, folder: str) -> None:
            if not bridge:
                box.move(uid, folder)
        box = Mailbox(self.settings, read_only=bridge)
        try:
            box.open()
            candidates = self._labeled(box) if bridge else self._own_mail(box)
        except MailError as exc:  # setup unfinished or Bridge closed: TODAY says what is missing, the log hears it once
            box.close()
            return self._problem(str(exc))
        self._problem("")
        with closing(box):
            for uid, raw, headers in candidates:
                if not bridge and sender_problem(headers, self.settings) is not None:
                    file(uid, IGNORED)
                    ignored += 1
                    continue
                key = _message_key(headers, raw)
                seen = f"mailread|{key}"
                if self.ledger.kv_get(seen):
                    file(uid, DONE)
                    continue
                if self._resting(key) or (blocked and self.ledger.kv_get(f"mailwait|{key}")):
                    waiting += 1 if blocked else 0
                    continue  # the model reads it once it can; nothing of it is stored
                mail = read_forward(box.message(uid), self.settings)
                category = screen_mail(mail, self.settings)
                if category:
                    if bridge:
                        self.ledger.kv_set(seen, "skipped")
                    file(uid, SKIPPED)
                    skipped[category] = skipped.get(category, 0) + 1
                    continue
                if not mail.forwarded:  # mail to yourself: Inbox lines, no model
                    lines = capture_lines(mail)
                    try:
                        if lines:
                            self.agent.append_to_inbox(lines)
                    except (RuntimeError, OSError) as exc:
                        messages.append(f"CHECK: could not add mail to yourself to the Inbox ({exc}). Trying again "
                                        "at the next check.")
                        continue
                    self.ledger.kv_set(seen, self.agent.today().isoformat())
                    file(uid, DONE)
                    captured += 1 if lines else 0
                    continue
                if blocked:
                    self.ledger.kv_set(f"mailwait|{key}", "1")
                    waiting += 1
                    continue
                try:
                    outcome = self.handle(vault, mail, key)
                except (ProviderError, ValueError, RuntimeError) as exc:
                    tries = self._failed(key)
                    if tries < MAX_TRIES:
                        messages.append(f"CHECK: could not read a message ({exc}). Trying again in {RETRY_MINUTES} "
                                        "minutes.")
                        continue
                    self.hk._propose("mail", f"mail|{key}", {
                        "kind": "manual", "title": self._title(mail), "ops": [],
                        "reason": f"The model could not read this forward {MAX_TRIES} times ({exc}). It is filed in "
                                  f"{DONE}; handle it by hand."})
                    outcome = "proposed"
                self.ledger.kv_set(seen, self.agent.today().isoformat())
                file(uid, DONE)
                read += 1
                quiet += outcome == "quiet"
            if bridge:
                messages += self._replies_in_sent(box)
        self._count(read, ignored, skipped, quiet, captured)
        if captured:
            messages.append(f"Mail: captured {_plural(captured, 'message')} to yourself in the Inbox")
        if waiting:
            messages.append(f"Mail: {_plural(waiting, 'message')} wait{'s' if waiting == 1 else ''} for the model "
                            f"({blocked})")
        if read:
            messages.append(f"Mail: read {_plural(read, 'message')}")
        if skipped:
            messages.append(f"Mail: skipped {_plural(sum(skipped.values()), 'message')} as sensitive")
        if ignored:
            messages.append(f"Mail: moved {_plural(ignored, 'message')} not from you to {IGNORED}")
        return messages

    def _problem(self, problem: str) -> list[str]:
        """Why mail can't be read right now ('' once it can). TODAY shows it; the log hears it when it changes."""
        previous = self.ledger.kv_get("mail_problem") or ""
        if problem != previous:
            self.ledger.kv_set("mail_problem", problem)
        return [f"CHECK: mail: {problem}"] if problem and problem != previous else []

    def _replies_in_sent(self, box: Mailbox) -> list[str]:
        """Bridge: your reply in Sent ticks the 'Reply to' action it answers (bookkeeping), and the replied wait then
        closes. Only the In-Reply-To and References lines of recent Sent mail are read."""
        vault = self.agent.vault()
        open_replies: dict[str, Task] = {}
        for task in vault.tasks:
            if task.is_open and task.task_id and task.task_id.startswith("reply-"):
                answered = self.ledger.kv_get(f"replyto|{task.task_id}")
                if answered:
                    open_replies[answered.strip().lower()] = task
        if not open_replies:
            return []
        uids = box.uids(SENT, readonly=True, since=self.agent.today() - timedelta(days=SENT_DAYS))[-SENT_LOOK:]
        ticked = 0
        for raw in box.header_fields(uids, ("IN-REPLY-TO", "REFERENCES")).values():
            headers = parse_headers(raw)
            named = " ".join(str(v) for name in ("In-Reply-To", "References") for v in headers.get_all(name) or [])
            for message_id in set(MSGID_RE.findall(named.lower())) & set(open_replies):
                task = open_replies.pop(message_id)
                where = self.hk._where(vault, task)
                op = {"op": "complete_task", "path": task.path, "match": task.raw,
                      "done": self.agent.today().isoformat()}
                self.agent.apply_ops([op], "auto", f"sent-{new_marker()}", f"Ticked '{task.title}': you replied")
                self.agent.log(f"Auto · {where}: ticked '{task.title}': your reply is in Sent")
                ticked += 1
        if not ticked:
            return []
        self.hk.settle_waits(self.agent.vault())  # the replied waits close now, not on the next scan
        return [f"Mail: found {_plural(ticked, 'reply', 'replies')} in Sent"]

    def _labeled(self, box: Mailbox) -> list[tuple[int, bytes, Message]]:
        """Bridge: the messages in the label folder the agent has not read yet, oldest first, looked at read-only."""
        uids = box.uids(self.settings.mail_folder, readonly=True)
        found: list[tuple[int, bytes, Message]] = []
        for start in range(0, len(uids), HEADER_BATCH):
            batch = box.headers(uids[start:start + HEADER_BATCH])
            for uid in uids[start:start + HEADER_BATCH]:
                if uid in batch:
                    headers = parse_headers(batch[uid])
                    if not self.ledger.kv_get(f"mailread|{_message_key(headers, batch[uid])}"):
                        found.append((uid, batch[uid], headers))
            if len(found) >= MAX_PER_CHECK:
                break
        return found[:MAX_PER_CHECK]

    def _own_mail(self, box: Mailbox) -> list[tuple[int, bytes, Message]]:
        """Up to MAX_PER_CHECK messages addressed to this agent, oldest first, judged on headers alone."""
        uids = box.uids("INBOX")
        found: list[tuple[int, bytes, Message]] = []
        for start in range(0, len(uids), HEADER_BATCH):
            batch = box.headers(uids[start:start + HEADER_BATCH])
            for uid in uids[start:start + HEADER_BATCH]:
                if uid in batch:
                    headers = parse_headers(batch[uid])
                    if addressed_to_agent(headers, self.settings):
                        found.append((uid, batch[uid], headers))
            if len(found) >= MAX_PER_CHECK:
                break
        return found[:MAX_PER_CHECK]

    # ------------------------------------------------------------ the model's turn
    def _model_problem(self) -> str | None:
        if not self.settings.remote_inference:
            return "remote inference is off"
        if not self.agent._provider_ready():
            return "the model key is missing"
        if not self.agent.budget.allows("mail"):
            return "the monthly model budget is used up"
        return None

    def _failed(self, key: str) -> int:
        stored = json.loads(self.ledger.kv_get(f"mailfail|{key}") or "{}")
        tries = int(stored.get("tries", 0)) + 1
        self.ledger.kv_set(f"mailfail|{key}", json.dumps({"tries": tries, "at": datetime.now().isoformat()}))
        return tries

    def _resting(self, key: str) -> bool:
        stored = json.loads(self.ledger.kv_get(f"mailfail|{key}") or "{}")
        try:
            return datetime.now() < datetime.fromisoformat(stored["at"]) + timedelta(minutes=RETRY_MINUTES)
        except (KeyError, ValueError):
            return False

    @staticmethod
    def _title(mail: Forward) -> str:
        name = mail.sender_name or mail.sender or "Mail"
        return _plain(f"{name}: {mail.subject}" if mail.subject else name, 150)

    def _open_waits(self, vault: Any, sender: set[str]) -> tuple[list[dict[str, Any]], dict[str, Task]]:
        """The open waits a mail may answer, private ones left out. from_sender marks the waits whose person is the
        mail's sender: the sender's name, a name of the contact who owns the address, or its first word."""
        waits: list[dict[str, Any]] = []
        lookup: dict[str, Task] = {}
        firsts = {name.split()[0] for name in sender if name.split()}
        for task in vault.waiting():
            project = vault.project_for_path(task.path)
            if task.has("#replied") or vault.is_private_task(task) or (project is not None and (
                    vault.is_private_project(project) or project.status in {"done", "dropped"})):
                continue
            person, what = task.waiting_parts()
            wait_id = f"w{len(waits) + 1}"
            folded = person.casefold()
            waits.append({"id": wait_id, "person": person, "what": what,
                          "where": project.key if project is not None else "SINGLE_ACTIONS",
                          "from_sender": bool(folded) and (folded in sender or folded in firsts)})
            lookup[wait_id] = task
        return waits[:80], lookup

    def handle(self, vault: Any, mail: Forward, key: str) -> str:
        """Ask the model what this forward asks of the user, and queue one proposal. 'proposed' or 'quiet'."""
        text = "\n".join(part for part in (mail.note, mail.subject, mail.body, *(a.text for a in mail.attachments))
                         if part)
        projects = [p for p in project_candidates(vault, text[:6000]) if p["status"] not in {"done", "dropped"}
                    and not vault.is_private_project(vault.projects[p["key"]])]
        areas = [a for a in area_candidates(vault) if not vault.areas[a["key"]].private]
        contacts = contact_notes(vault, self.settings)
        sender = {name.casefold() for c in contacts if mail.sender in c["emails"] for name in c["names"]}
        sender |= {mail.sender_name.casefold()} if mail.sender_name else set()
        waits, lookup = self._open_waits(vault, sender)
        self._contact(vault, mail, contacts)
        mail_day = mail.date.date() if mail.date else self.agent.today()
        value = self.agent.provider.mail({
            "from_name": mail.sender_name, "from": mail.sender, "subject": mail.subject,
            "date": mail.date.isoformat(timespec="minutes") if mail.date else "", "note": mail.note,
            "to": [*mail.to, *mail.cc][:20], "body": mail.body[:MAX_BODY_CHARS],
            "attachments": [{"name": a.filename, "text": a.text} for a in mail.attachments if a.filename or a.text]},
            mail_day, projects, areas, waits)
        proposal = self.compile(vault, mail, value, text, projects, areas, lookup, mail_day)
        if proposal is None:
            return "quiet"
        self.hk._propose("mail", f"mail|{key}", proposal)
        return "proposed"

    def _contact(self, vault: Any, mail: Forward, contacts: list[dict[str, Any]]) -> None:
        """A sender the agent does not know yet: ask once, then keep the address in 04_REFERENCE/CONTACTS."""
        address = mail.sender
        if not mail.forwarded or "@" not in address or _own(address, self.settings) \
                or any(address in contact["emails"] for contact in contacts):
            return
        name = re.sub(r"[\[\],#|]", " ", _plain(mail.sender_name, 80)).strip() or address.split("@")[0]
        name = re.sub(r"\s+", " ", name)
        key = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper() or "CONTACT"
        base = f"{self.settings.reference_dir}/CONTACTS"
        taken = {rel.rsplit("/", 1)[-1][:-3].upper() for rel in vault.notes if rel.startswith(base + "/")}
        new_key, number = key, 2
        while new_key in taken:
            new_key, number = f"{key}_{number}", number + 1
        text = "\n".join(["---", "type: contact", f"aliases: [{name}]", f"emails: [{address}]", "---", f"# {new_key}",
                          "", f"- Email: {address}",
                          f"- Added {self.agent.today().isoformat()} from mail you labeled or forwarded.", ""])
        create = [{"op": "create_file", "path": f"{base}/{new_key}.md", "text": text}]
        same = next((c for c in contacts if name.casefold() in {n.casefold() for n in c["names"]}
                     or c["key"].upper() == key), None)
        if same is None:
            choices = [{"label": f"Add {name}", "ops": create}]
        else:
            emails = ", ".join([*same["emails"], address])
            choices = [{"label": f"Add to {same['key']}", "ops": [
                           {"op": "set_property", "path": same["rel"], "key": "emails", "value": f"[{emails}]"}]},
                       {"label": f"New contact {new_key}", "ops": create}]
        self.hk._propose("contact", f"contact|{address}", {
            "kind": "contact", "title": f"New contact: {name} <{address}>", "summary": f"{base}/", "ops": [],
            "reason": f"{name} wrote mail you labeled or forwarded. Confirm once, and the agent knows this address "
                      "from then on; Leave it never asks again.", "choices": choices})

    def compile(self, vault: Any, mail: Forward, value: Any, text: str, projects: list[dict], areas: list[dict],
                lookup: dict[str, Task], mail_day: date) -> dict[str, Any] | None:
        """Python checks every item the model found against the mail itself; each one that holds becomes a box."""
        if not isinstance(value, dict):
            raise ValueError("the model answer is not an object")
        today = self.agent.today()
        squashed = _squash(text)
        target = str(value.get("target_key") or "").strip()
        project = vault.projects.get(target) if target in {p["key"] for p in projects} else None
        area = target if project is None and target in {a["key"] for a in areas} else ""
        items: list[dict[str, Any]] = []
        unread = [f"{a.filename or 'A PDF'} was not read: {a.problem}." for a in mail.attachments if a.problem]
        notes: list[str] = list(unread)
        dropped = 0

        def supported(evidence: Any) -> bool:
            evidence = _plain(evidence, 300)
            return bool(evidence) and _squash(evidence) in squashed

        def compiled(fields: dict[str, str], extra: dict[str, Any] | None = None
                     ) -> tuple[list[dict[str, Any]], str, str, Any]:
            checked = validate_interpretation(blank_draft(
                project_key=project.key if project is not None else "", area_key=area,
                explanation="Read from mail", lesson="Mail item", **fields), text, projects, areas, [])
            draft, _, _ = draft_from_interpretation(vault, checked, mail_day, {})
            draft.update(extra or {})
            ops, summary, title = compile_ops(vault, draft, capture=checked["title"] or checked["what"],
                                              reference=today, marker=new_marker())
            return ops, summary, title, draft["dates"].get("due")

        def undated(fields: dict[str, str]) -> tuple[list[dict[str, Any]], str, str, Any] | None:
            try:
                return compiled(fields)
            except ValueError:
                if not fields.get("due_evidence"):
                    return None
            try:
                found = compiled({**fields, "due_evidence": ""})
            except ValueError:
                return None
            notes.append(f"Could not read the date '{fields['due_evidence']}'; check it.")
            return found

        def action(fields: dict[str, str]) -> None:
            nonlocal dropped
            found = undated(fields)
            if found is None:
                dropped += 1
                return
            ops, summary, title, due = found
            label = f"Action · {title}" + (f" · due {_fmt(due)}" if due else "") + f" → {summary}"
            items.append({"label": label, "ops": ops})

        for entry in (value.get("actions") or [])[:20]:
            if not isinstance(entry, dict) or not supported(entry.get("evidence")):
                dropped += 1
                continue
            action({"route": "next_action", "title": _plain(entry.get("title"), 150),
                    "context": entry.get("context") or "#computer", "due_evidence": _plain(entry.get("due_evidence"), 80)})
        offered = list(dict.fromkeys(event for a in mail.attachments for event in a.events))[:10]
        for invite in offered:  # an invitation says exactly when; the model's copy of it is left out below
            try:
                ops, summary, shown = self._invite_ops(vault, invite, today)
                items.append({"label": f"Invite · {shown} → {summary}", "ops": ops})
            except ValueError as exc:
                notes.append(f"Invite '{_plain(invite.summary, 80)}' skipped: {exc}.")
        for entry in ([] if offered else (value.get("events") or [])[:10]):
            if not isinstance(entry, dict) or not supported(entry.get("evidence")):
                dropped += 1
                continue
            title, day, time = (_plain(entry.get(k), 150) for k in ("title", "day_evidence", "time_evidence"))
            place = _plain(entry.get("place"), 120)
            try:
                ops, summary, shown, _ = compiled({"route": "calendar_event", "title": title, "context": "#calls",
                                                   "due_evidence": day, "time_evidence": time,
                                                   "end_evidence": _plain(entry.get("end_evidence"), 80)},
                                                  {"place": place if supported(place) else ""})
                items.append({"label": f"Event · {shown} → {summary}", "ops": ops})
            except ValueError:  # the time is unclear or the day has passed: keep it as a dated action
                action({"route": "next_action", "title": f"{title} at {time}" if time else title,
                        "context": "#calls", "due_evidence": day})
        for entry in (value.get("waits") or [])[:10]:
            if not isinstance(entry, dict) or not supported(entry.get("evidence")):
                dropped += 1
                continue
            found = undated({"route": "waiting_for", "person": _plain(entry.get("person"), 120),
                             "what": _plain(entry.get("what"), 200), "context": "#anywhere",
                             "due_evidence": _plain(entry.get("due_evidence"), 80)})
            if found is None:
                dropped += 1
                continue
            ops, summary, title, _ = found
            items.append({"label": f"Wait · {title} → {summary}", "ops": ops})
        claimed: set[str] = set()
        drafts: list[dict[str, Any]] = []
        for entry in (value.get("answers") or [])[:10]:
            wait = lookup.get(str(entry.get("wait_id") or "")) if isinstance(entry, dict) else None
            if wait is None or wait.raw in claimed or not supported(entry.get("evidence")):
                dropped += 1
                continue
            claimed.add(wait.raw)
            ops, label, draft = self._replied(vault, wait, today, mail)
            items.append({"label": label, "ops": ops})
            if draft is not None:
                drafts.append(draft)
        if dropped:
            notes.append(f"Dropped {_plural(dropped, 'item')} the mail did not support.")
        name = mail.sender_name or mail.sender or "someone"
        sent = f", sent {_fmt(mail.date.date())}" if mail.date else ""
        if not items:  # nothing to act on, unless a PDF went unread: then you look at it yourself
            return {"kind": "manual", "title": self._title(mail), "ops": [], "reason": " ".join(
                [f"Mail from {_plain(name, 80)}{sent}.", *unread, "Nothing else in it asks anything of you; open "
                 "the attachment yourself."])} if unread else None
        base_ops = []
        if project is not None:
            subject = _plain(mail.subject, 120).replace("[[", "").replace("]]", "")
            base_ops.append({"op": "append_log", "path": project.rel,
                             "entry": f"{today.isoformat()} Mail from {_plain(name, 80)}: {subject}."})
        reason = " ".join(part for part in [f"Mail from {_plain(name, 80)}{sent}.", _plain(value.get("summary"), 300),
                                            _plain(value.get("explanation"), 300), *notes,
                                            f"{self.settings.job_model('mail')} read it; Python checked every quote "
                                            "and date."] if part)
        return {"kind": "mail", "title": self._title(mail), "reason": reason,
                "summary": _plural(len(items), "item") + (f" · logged in {project.key}" if project else ""),
                "base_ops": base_ops, "items": items, "drafts": drafts,
                "ops": [*base_ops, *(op for i in items for op in i["ops"])]}

    @staticmethod
    def _invite_ops(vault: Any, invite: Invite, today: date) -> tuple[list[dict[str, Any]], str, str]:
        """An invitation's event, as the calendar route takes it: added after approval (Radicale) or, for Proton, an
        action to add it by hand. An all-day event is always added by hand."""
        if not invite.all_day:
            return compile_ops(vault, {"route": "calendar_event", "title": invite.summary, "place": invite.location,
                                       "event_day": invite.start.date(), "start_time": invite.start.time(),
                                       "end": invite.end, "repeats": invite.repeats, "extra_tags": []},
                               capture=invite.summary, reference=today, marker=new_marker())
        last = (invite.end - timedelta(days=1)).date() if invite.end.date() > invite.start.date() else invite.start.date()
        if last < today:
            raise ValueError("That day has passed")
        first = invite.start.date()
        when = f"all day {_fmt(first)}" if last == first else f"{_fmt(first)} to {_fmt(last)}"
        what = invite.summary + (f" ({invite.location})" if invite.location else "")
        return calendar_by_hand(vault, what, when + (", repeats" if invite.repeats else ""), reference=today,
                                marker=new_marker())

    def _replied(self, vault: Any, wait: Task, today: date,
                 mail: Forward) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None]:
        """The wait stays open, marked #replied, and waits (⛔) on a new 'Reply to' action that carries its 🆔:
        ticking that action closes the wait (Housekeeper.settle_waits). A follow-up action the wait still waited
        on is cancelled, since they answered. The reply gets a draft; through Bridge it is saved into Proton Drafts
        on approval, threaded to this mail."""
        person, what = wait.waiting_parts()
        reply_id = f"reply-{new_marker()[:8]}"
        line = with_tag(wait.raw, "#replied")
        open_tasks = {task.task_id: task for task in vault.tasks if task.task_id and task.is_open}
        dropped = []
        for task_id in wait.depends_on:
            if task_id.startswith("fu-"):
                line = without_dependency(line, task_id)
                if task_id in open_tasks:
                    dropped.append({"op": "cancel_task", "path": open_tasks[task_id].path,
                                    "match": open_tasks[task_id].raw, "done": today.isoformat()})
        line = with_dependency(line, reply_id)
        project = vault.project_for_path(wait.path)
        owner = vault.area_for_path(wait.path)
        area = None if project is not None else next((link for link in wait.links if link in vault.areas),
                                                     owner.key if owner is not None else None)
        title = f"Reply to {person} about {what}" if person else f"Reply about {what}"
        ops, summary, shown = compile_ops(vault, {"route": "next_action", "title": title, "context": "#computer",
                                                 "project": project, "area": area, "dates": {}, "extra_tags": [],
                                                 "explicit_next": True},
                                          capture=title, reference=today, marker=new_marker())
        ops[0]["line"] = with_id(ops[0]["line"], reply_id)
        ops = [{"op": "replace_line", "path": wait.path, "match": wait.raw, "line": line}, *dropped, *ops]
        if mail.message_id:  # through Bridge, your reply in Sent names this mail and ticks the action
            self.ledger.kv_set(f"replyto|{reply_id}", mail.message_id)
        to = mail_address(mail.sender_name, mail.sender)
        draft = write_draft(self.agent, vault, "reply", {
            "to_name": mail.sender_name or person, "person": person, "what": what, "their_subject": mail.subject,
            "their_mail": mail.body[:4000], "note": mail.note})
        shown_draft = None
        if draft is not None:
            bridge = self.settings.mail_source == "bridge"
            if bridge:
                ops.append(draft_op(draft, to, mail.message_id))
            shown_draft = {"title": shown, "to": to, "saved": bridge, **draft}
        return (ops, f"Replied · {person + ': ' if person else ''}{what} → marks the wait replied"
                     f"{', cancels your follow-up' if dropped else ''} and adds '{shown}' ({summary})", shown_draft)

    def _count(self, read: int, ignored: int, skipped: dict[str, int], quiet: int = 0, captured: int = 0) -> None:
        if not (read or ignored or skipped or captured):
            return
        key = f"mail_day|{self.agent.today().isoformat()}"
        stored = json.loads(self.ledger.kv_get(key) or "{}")
        stored["read"] = stored.get("read", 0) + read
        stored["quiet"] = stored.get("quiet", 0) + quiet
        stored["ignored"] = stored.get("ignored", 0) + ignored
        stored["captured"] = stored.get("captured", 0) + captured
        kinds = stored.setdefault("skipped", {})
        for category, count in skipped.items():
            kinds[category] = kinds.get(category, 0) + count
        self.ledger.kv_set(key, json.dumps(stored, sort_keys=True))
