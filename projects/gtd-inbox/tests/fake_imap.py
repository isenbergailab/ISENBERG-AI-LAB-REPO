"""A small IMAP server over TLS for the mail tests: just enough of RFC 3501 (plus MOVE and UIDPLUS) for imaplib
and the agent.

Mail lives in memory; nothing real is read. tls_fixtures generates temporary certificates
for a loopback-only fake server; temporary keys are removed at process exit.
"""
from __future__ import annotations

import email
import re
import socketserver
import ssl
import threading
from tls_fixtures import FIXTURES

CA_FILE = str(FIXTURES / "imap-ca.pem")
TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|(\([^)]*\))|(\S+)')
HEADER_END_RE = re.compile(rb"\r?\n\r?\n")


def _tokens(text: str) -> list[str]:
    return [re.sub(r"\\(.)", r"\1", m.group(1)) if m.group(1) is not None else (m.group(2) or m.group(3))
            for m in TOKEN_RE.finditer(text)]


def _uid_set(text: str, known: list[int]) -> list[int]:
    wanted: set[int] = set()
    top = max(known, default=0)
    for part in text.split(","):
        low, _, high = part.partition(":")
        start = top if low == "*" else int(low)
        end = start if not high else (top if high == "*" else int(high))
        wanted.update(range(min(start, end), max(start, end) + 1))
    return [uid for uid in known if uid in wanted]


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:  # a refused handshake is part of some tests
        pass


class FakeImap:
    """Start it, point [mail] at 127.0.0.1 and `port`, and trust CA_FILE (SSL_CERT_FILE)."""

    def __init__(self, user: str, password: str, move: bool = True, tls: str = "tls") -> None:
        """tls: "tls" (TLS from the first byte), "starttls" (plain until STARTTLS) or "none" (never TLS)."""
        self.user, self.password = user, password
        self.tls = tls
        self.capabilities = "IMAP4rev1 UIDPLUS" + (" MOVE" if move else "") + (" STARTTLS" if tls == "starttls" else "")
        self.folders: dict[str, list[dict]] = {"INBOX": []}
        self.commands: list[tuple[str, list[str]]] = []  # every command seen; LOGIN's password is masked
        self.fetched: list[tuple[int, str]] = []          # (uid, fetch items) for every message served
        self.next_uid = 1
        self.uidvalidity = 1
        self._lock = threading.Lock()
        self._server: _Server | None = None
        self.port = 0

    # ------------------------------------------------------------ test helpers
    def add(self, raw: bytes, folder: str = "INBOX") -> int:
        with self._lock:
            uid = self.next_uid
            self.next_uid += 1
            self.folders.setdefault(folder, []).append({"uid": uid, "data": raw, "flags": set()})
            return uid

    def uids(self, folder: str = "INBOX") -> list[int]:
        return [m["uid"] for m in self.folders.get(folder, [])]

    def subjects(self, folder: str = "INBOX") -> list[str]:
        return [str(email.message_from_bytes(m["data"])["Subject"]) for m in self.folders.get(folder, [])]

    def names(self) -> list[str]:
        return [name for name, _ in self.commands]

    def start(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(FIXTURES / "imap-server.pem", FIXTURES / "imap-server.key")
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                conn = self.request
                if fake.tls == "tls":
                    try:
                        conn = context.wrap_socket(conn, server_side=True)
                    except (ssl.SSLError, OSError):  # the client refused the certificate
                        return
                upgrade = None
                with conn:
                    with conn.makefile("rb") as rfile, conn.makefile("wb") as wfile:
                        try:
                            upgrade = fake._session(rfile, wfile, greet=True)
                        except (OSError, ValueError):
                            return
                    if upgrade is None:
                        return
                    try:  # STARTTLS: the same session continues over TLS
                        secure = context.wrap_socket(conn, server_side=True)
                    except (ssl.SSLError, OSError):
                        return
                    with secure, secure.makefile("rb") as rfile, secure.makefile("wb") as wfile:
                        try:
                            fake._session(rfile, wfile, greet=False, secure=True)
                        except (OSError, ValueError):
                            pass

        self._server = _Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # ------------------------------------------------------------ protocol
    def _session(self, rfile, wfile, greet: bool = True, secure: bool = False) -> bool | None:
        """Serve one connection. Returns True when the client asked for STARTTLS (the caller upgrades)."""
        def send(line: str | bytes) -> None:
            wfile.write((line.encode("utf-8") if isinstance(line, str) else line) + b"\r\n")
            wfile.flush()

        if greet:
            send(f"* OK [CAPABILITY {self.capabilities}] Fake IMAP ready")
        secure = secure or self.tls == "tls"
        authed = False
        selected: str | None = None
        while True:
            line = rfile.readline()
            if not line:
                return
            text = line.decode("utf-8").rstrip("\r\n")
            literal = re.search(r"\{(\d+)\}$", text)
            data = b""
            if literal:  # APPEND carries the message as a literal after a go-ahead
                send("+ Ready for literal data")
                data = rfile.read(int(literal.group(1)))
                rfile.readline()
                text = text[:literal.start()].rstrip()
            tag, _, rest = text.partition(" ")
            name, _, argtext = rest.partition(" ")
            name = name.upper()
            args = _tokens(argtext)
            self.commands.append((name, [args[0], "***"] if name == "LOGIN" and args else args))
            with self._lock:
                if name == "STARTTLS" and self.tls == "starttls" and not secure:
                    send(f"{tag} OK Begin TLS negotiation now")
                    return True
                if name == "CAPABILITY":
                    send(f"* CAPABILITY {self.capabilities}")
                    send(f"{tag} OK CAPABILITY completed")
                elif name == "NOOP":
                    send(f"{tag} OK NOOP completed")
                elif name == "LOGOUT":
                    send("* BYE Fake IMAP logging out")
                    send(f"{tag} OK LOGOUT completed")
                    return None
                elif name == "LOGIN":
                    if args[:2] == [self.user, self.password]:
                        authed = True
                        send(f"{tag} OK LOGIN completed")
                    else:
                        send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials")
                elif not authed:
                    send(f"{tag} BAD Log in first")
                elif name in ("SELECT", "EXAMINE"):
                    folder = self._folder(args[0] if args else "")
                    if folder is None:
                        send(f"{tag} NO [NONEXISTENT] No such folder")
                        continue
                    selected = folder
                    send(f"* {len(self.folders[folder])} EXISTS")
                    send("* 0 RECENT")
                    send(f"* OK [UIDVALIDITY {self.uidvalidity}] UIDs valid")
                    send(f"* OK [UIDNEXT {self.next_uid}] Predicted next UID")
                    send("* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
                    send(f"{tag} OK [{'READ-ONLY' if name == 'EXAMINE' else 'READ-WRITE'}] {name} completed")
                elif name == "LIST":
                    pattern = args[1] if len(args) > 1 else "*"
                    for folder in self.folders:
                        if pattern in ("*", folder):
                            send(f'* LIST (\\HasNoChildren) "/" "{folder}"')
                    send(f"{tag} OK LIST completed")
                elif name == "CREATE":
                    if self._folder(args[0]) is not None:
                        send(f"{tag} NO [ALREADYEXISTS] Folder exists")
                    else:
                        self.folders[args[0]] = []
                        send(f"{tag} OK CREATE completed")
                elif name == "APPEND":
                    folder = self._folder(args[0]) or args[0]
                    flags = set(args[1].strip("()").split()) if len(args) > 1 and args[1].startswith("(") else set()
                    self.folders.setdefault(folder, []).append({"uid": self.next_uid, "data": data, "flags": flags})
                    self.next_uid += 1
                    send(f"{tag} OK APPEND completed")
                elif name == "EXPUNGE" and selected:
                    self._expunge(selected, None, send)
                    send(f"{tag} OK EXPUNGE completed")
                elif name == "UID" and selected and args:
                    self._uid(tag, args[0].upper(), args[1:], selected, send, argtext)
                else:
                    send(f"{tag} BAD {name} is not supported by the fake server")

    def _folder(self, name: str) -> str | None:
        if name.upper() == "INBOX":
            return "INBOX"
        return name if name in self.folders else None

    def _uid(self, tag: str, command: str, args: list[str], selected: str, send, raw: str = "") -> None:
        messages = self.folders[selected]
        if command == "SEARCH":
            send("* SEARCH" + "".join(f" {m['uid']}" for m in messages))
            send(f"{tag} OK SEARCH completed")
            return
        uids = _uid_set(args[0], [m["uid"] for m in messages]) if args else []
        if command == "FETCH":
            items = re.sub(r"^\S+\s+\S+\s+", "", raw).strip()  # what follows 'FETCH <set>', as sent
            items = (items[1:-1] if items.startswith("(") and items.endswith(")") else items).upper()
            fields = re.match(r"BODY\.PEEK\[HEADER\.FIELDS \(([^)]*)\)\]$", items)
            for seq, m in enumerate(messages, 1):
                if m["uid"] not in uids:
                    continue
                self.fetched.append((m["uid"], "HEADER.FIELDS" if fields else items))
                if fields:
                    message = email.message_from_bytes(m["data"])
                    names = fields.group(1).split()
                    lines = [f"{name}: {value}" for name in names for value in message.get_all(name) or []]
                    data = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
                    label = f"BODY[HEADER.FIELDS ({' '.join(names)})]"
                elif items == "BODY.PEEK[HEADER]":
                    data, label = HEADER_END_RE.split(m["data"], 1)[0] + b"\r\n\r\n", "BODY[HEADER]"
                elif items == "BODY.PEEK[]":
                    data, label = m["data"], "BODY[]"
                else:
                    send(f"{tag} BAD fetch items {items} are not supported by the fake server")
                    return
                send(f"* {seq} FETCH (UID {m['uid']} {label} {{{len(data)}}}".encode("utf-8") + b"\r\n" + data + b")")
            send(f"{tag} OK FETCH completed")
        elif command in ("MOVE", "COPY"):
            if command == "MOVE" and "MOVE" not in self.capabilities:
                send(f"{tag} BAD MOVE is not supported")
                return
            target = self._folder(args[1] if len(args) > 1 else "")
            if target is None:
                send(f"{tag} NO [TRYCREATE] No such folder")
                return
            for m in [m for m in messages if m["uid"] in uids]:
                self.folders[target].append({"uid": self.next_uid, "data": m["data"], "flags": set(m["flags"])})
                self.next_uid += 1
            if command == "MOVE":
                for m in messages:
                    if m["uid"] in uids:
                        m["flags"].add("\\Deleted")
                self._expunge(selected, uids, send)
            send(f"{tag} OK {command} completed")
        elif command == "STORE":
            flags = set(args[2].strip("()").split()) if len(args) > 2 else set()
            for seq, m in enumerate(messages, 1):
                if m["uid"] in uids:
                    if args[1].upper().startswith("+"):
                        m["flags"] |= flags
                    elif args[1].upper().startswith("-"):
                        m["flags"] -= flags
                    else:
                        m["flags"] = flags
            send(f"{tag} OK STORE completed")
        elif command == "EXPUNGE":
            self._expunge(selected, uids, send)
            send(f"{tag} OK EXPUNGE completed")
        else:
            send(f"{tag} BAD UID {command} is not supported by the fake server")

    def _expunge(self, folder: str, only: list[int] | None, send) -> None:
        messages = self.folders[folder]
        for seq in range(len(messages), 0, -1):
            m = messages[seq - 1]
            if "\\Deleted" in m["flags"] and (only is None or m["uid"] in only):
                del messages[seq - 1]
                send(f"* {seq} EXPUNGE")
