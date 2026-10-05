"""The mailbox session: Proton through Bridge (docs/PRIVACY.md) or the agent's own inbox (docs/PRIVACY.md). IMAP over TLS,
standard library only.

Through Bridge the password opens the user's whole mailbox, so this class is the wall: it opens only the GTD label and
Sent, read-only (EXAMINE), writes only into Drafts, and refuses everything else before a command reaches Bridge.
The Private folder is never opened. The agent never sends mail: nothing here or anywhere else speaks SMTP. Every
error is worded as the fix and never carries the password.
"""
from __future__ import annotations

import imaplib
import re
import ssl
import time
from typing import Any

from .core import Settings
from .credentials import get_secret

SECRET = "agent-mail"
SENT = "Sent"      # Bridge: the agent reads only the reply headers here
DRAFTS = "Drafts"  # Bridge: the one folder the agent writes into (APPEND, never sent)
TIMEOUT_SECONDS = 30
UID_RE = re.compile(rb"UID (\d+)")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class MailError(Exception):
    """A mail problem the user can fix, worded as the fix."""


def _quoted(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Mailbox:
    """A logged-in session with the agent inbox: `with Mailbox(settings) as box: box.uids()`."""

    def __init__(self, settings: Settings, read_only: bool = False) -> None:
        """read_only: this session may look but never move or create anything (Bridge: the user's own mailbox)."""
        self.settings = settings
        self.read_only = read_only
        self.imap: imaplib.IMAP4_SSL | None = None
        self.capabilities: set[str] = set()
        self._folders: set[str] = set()

    def __enter__(self) -> "Mailbox":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> None:
        s = self.settings
        password = get_secret(SECRET, s.state_dir)
        if not password:
            if s.mail_source == "bridge":
                raise MailError(f"No Bridge password is saved. Copy it from Bridge (your account › Mailbox details › "
                                f"Password), then run: run.ps1 set-secret {SECRET}")
            raise MailError(f"No app password is saved for the agent inbox. Run: run.ps1 set-secret {SECRET}")
        where = f"{s.mail_host}:{s.mail_port}"
        context = ssl.create_default_context()
        if s.mail_ca_file:
            try:  # trust this certificate, and only it, for this connection: Bridge's self-signed one
                context = ssl.create_default_context(cafile=s.mail_ca_file)
            except (OSError, ssl.SSLError):
                raise MailError(f"Can't read the certificate in [mail] ca_file ({s.mail_ca_file}). In Bridge: Settings "
                                "› Advanced settings › Export TLS certificates, into that file's folder.") from None
            context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        imap: imaplib.IMAP4 | None = None
        try:
            try:
                if s.mail_security == "starttls":
                    imap = imaplib.IMAP4(s.mail_host, s.mail_port, timeout=TIMEOUT_SECONDS)
                    if "STARTTLS" not in imap.capabilities:
                        raise MailError(f"{s.mail_host} didn't offer STARTTLS; the agent never logs in without TLS. "
                                        "Check [mail] security and port.")
                    imap.starttls(ssl_context=context)
                else:
                    imap = imaplib.IMAP4_SSL(s.mail_host, s.mail_port, ssl_context=context, timeout=TIMEOUT_SECONDS)
            except BaseException:
                if imap is not None:
                    try:
                        imap.shutdown()
                    except OSError:
                        pass
                raise
            self.imap = imap
        except MailError:
            raise
        except ssl.SSLCertVerificationError as exc:
            raise MailError(f"{s.mail_host} sent a certificate this computer doesn't trust ({exc.verify_message}). "
                            "Check [mail] host; the agent never turns certificate checks off.") from None
        except ssl.SSLError as exc:
            raise MailError(f"TLS with {where} failed ({exc.reason or exc}). Check [mail] port: the agent needs "
                            "IMAP over TLS, usually 993.") from None
        except TimeoutError:
            if self._bridge():
                raise MailError(self._bridge_closed(where)) from None
            raise MailError(f"{where} didn't answer within {TIMEOUT_SECONDS} seconds. Check [mail] host and port, "
                            "and the network.") from None
        except (OSError, imaplib.IMAP4.error) as exc:
            if self._bridge():
                raise MailError(self._bridge_closed(where)) from None
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            raise MailError(f"Can't reach {where} ({reason}). Check [mail] host and port, and the network.") from None
        try:
            self.imap.login(s.mail_user, password)
        except (OSError, imaplib.IMAP4.abort):
            self.close()
            raise MailError(f"The connection to {where} dropped during login. Try again; if it keeps happening, "
                            "check the network.") from None
        except imaplib.IMAP4.error:
            self.close()
            raise MailError(f"{s.mail_host} refused the login for {s.mail_user}. Make a new app password for the "
                            f"agent, then run: run.ps1 set-secret {SECRET}") from None
        except UnicodeEncodeError:
            self.close()
            raise MailError("The saved app password has characters IMAP can't send. Make a new app password for "
                            f"the agent, then run: run.ps1 set-secret {SECRET}") from None
        typ, data = self.imap.capability()  # servers often list more once you are logged in
        if typ == "OK" and data and data[-1]:
            self.capabilities = set(data[-1].decode("ascii", "replace").upper().split())

    def _session(self) -> imaplib.IMAP4_SSL:
        if self.imap is None:
            raise MailError("The agent inbox isn't open")
        return self.imap

    def _bridge(self) -> bool:
        return self.settings.mail_source == "bridge"

    @staticmethod
    def _bridge_closed(where: str) -> str:
        return (f"Can't reach Proton Mail Bridge at {where}. Open Bridge and leave it running. If it is open, its "
                "IMAP port (Bridge › Settings › Advanced settings › Default ports) must match [mail] port.")

    def _may_open(self, folder: str, readonly: bool) -> None:
        """Through Bridge: the GTD label and Sent, read-only, and nothing else."""
        if not self._bridge():
            return
        if folder not in {self.settings.mail_folder, SENT}:
            raise MailError(f"The agent never opens {folder}. Through Bridge it reads only {self.settings.mail_folder} "
                            f"and the reply headers in {SENT}.")
        if not readonly:
            raise MailError("Through Bridge the agent only looks: it opens folders read-only")

    def uids(self, folder: str = "INBOX", readonly: bool = False, since: Any = None) -> list[int]:
        """The folder's messages, oldest first, as UIDs (only those since a date, when given). It stays selected.
        A read-only look (EXAMINE) changes nothing."""
        self._may_open(folder, readonly)
        imap = self._session()
        typ, _ = imap.select(_quoted(folder), readonly=readonly)
        if typ != "OK":
            if folder.startswith("Labels/"):  # Bridge shows Proton's labels as folders under Labels/
                raise MailError(f"Proton has no {folder[7:]} label yet. Create it in Proton Mail › Settings › "
                                "All settings › Folders and labels.")
            raise MailError(f"The mailbox has no {folder} folder")
        if since is not None:
            typ, data = imap.uid("SEARCH", "SINCE", f"{since.day:02d}-{MONTHS[since.month - 1]}-{since.year}")
        else:
            typ, data = imap.uid("SEARCH", "ALL")
        if typ != "OK":
            raise MailError(f"The agent inbox wouldn't list {folder}")
        return sorted(int(uid) for uid in (data[0] or b"").split())

    def headers(self, uids: list[int]) -> dict[int, bytes]:
        """Each message's header block without its body, left unread (BODY.PEEK[HEADER])."""
        return self._fetch(uids, "BODY.PEEK[HEADER]")

    def header_fields(self, uids: list[int], fields: tuple[str, ...]) -> dict[int, bytes]:
        """Only the named header lines of each message, left unread."""
        return self._fetch(uids, f"BODY.PEEK[HEADER.FIELDS ({' '.join(fields)})]")

    def message(self, uid: int) -> bytes:
        """The whole message, left unread (BODY.PEEK[])."""
        found = self._fetch([uid], "BODY.PEEK[]")
        if uid not in found:
            raise MailError("A message vanished from the agent inbox while the agent read it")
        return found[uid]

    def _fetch(self, uids: list[int], items: str) -> dict[int, bytes]:
        if not uids:
            return {}
        typ, data = self._session().uid("FETCH", ",".join(str(uid) for uid in uids), f"({items})")
        if typ != "OK":
            raise MailError("The agent inbox wouldn't hand over mail")
        found: dict[int, bytes] = {}
        waiting: bytes | None = None  # a literal whose UID comes after it
        for part in data:
            if isinstance(part, tuple):
                match = UID_RE.search(part[0])
                if match:
                    found[int(match.group(1))] = part[1]
                    waiting = None
                else:
                    waiting = part[1]
            elif isinstance(part, bytes) and waiting is not None:
                match = UID_RE.search(part)
                if match:
                    found[int(match.group(1))] = waiting
                waiting = None
        return found

    def move(self, uid: int, folder: str) -> None:
        """Move one message out of the selected folder, creating the target folder the first time."""
        if self._bridge():
            raise MailError("Through Bridge the agent never moves mail")
        if self.read_only:
            raise MailError("This mailbox is read-only for the agent; nothing may move")
        imap = self._session()
        self._ensure(folder)
        if "MOVE" in self.capabilities:
            typ, _ = imap.uid("MOVE", str(uid), _quoted(folder))
        else:
            typ, _ = imap.uid("COPY", str(uid), _quoted(folder))
            if typ == "OK":
                imap.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)")
                typ, _ = imap.uid("EXPUNGE", str(uid)) if "UIDPLUS" in self.capabilities else imap.expunge()
        if typ != "OK":
            raise MailError(f"The agent inbox wouldn't move a message to {folder}")

    def save_draft(self, raw: bytes, folder: str = DRAFTS) -> None:
        """The one write the agent makes in the user's own mailbox: a draft for him to send himself. Never sent."""
        if self._bridge() and folder != DRAFTS:
            raise MailError(f"Through Bridge the agent writes only into {DRAFTS}")
        typ, _ = self._session().append(_quoted(folder), "(\\Draft)", imaplib.Time2Internaldate(time.time()), raw)
        if typ != "OK":
            raise MailError(f"The mailbox wouldn't take a draft into {folder}")

    def _ensure(self, folder: str) -> None:
        if folder in self._folders:
            return
        imap = self._session()
        typ, data = imap.list('""', _quoted(folder))
        if not (typ == "OK" and data and data[0] is not None):
            typ, _ = imap.create(_quoted(folder))
            if typ != "OK":
                raise MailError(f"The agent inbox wouldn't create a {folder} folder")
        self._folders.add(folder)

    def close(self) -> None:
        imap, self.imap = self.imap, None
        if imap is None:
            return
        try:
            imap.logout()
        except (OSError, imaplib.IMAP4.error):
            try:
                imap.shutdown()
            except OSError:
                pass
