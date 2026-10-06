"""Settings, safe file I/O, locks and Inbox capture parsing."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 only
    _toml = None

OPENROUTER_CHAT = "https://openrouter.ai/api/v1/chat/completions"
MODEL_JOBS = ("interpret", "split", "documents", "decided", "outcome", "vague_check", "someday_topics", "mail",
              "draft", "after_event")
SECRET_RE = re.compile(r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|(?:password|passwd|api[_ -]?key|secret|token)\s*[:=]\s*\S+)")
DONE_SUFFIX_RE = re.compile(r"\s+(?:✅|❌)\s*\d{4}-\d{2}-\d{2}\s*$")
REVIEW_BEGIN = "<!-- gtd-agent:begin -->"
REVIEW_END = "<!-- gtd-agent:end -->"
CONTEXTS = ("#computer", "#calls", "#anywhere", "#errands")
# Proposal kinds that can move from judgment to bookkeeping ([trust] bookkeeping): each applies with no answer.
TRUSTABLE = ("next_action", "waiting_for", "project_note", "new_project", "someday_maybe", "completion_report",
             "calendar_event", "decision", "document", "follow_up", "promotion", "someday_topic", "research_report",
             "drop_knock", "outcome_change", "current_state", "mail")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def digest(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def now_local() -> datetime:
    return datetime.now()


# ---------------------------------------------------------------- settings
def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        escapes = {"\\\\": "\\", '\\"': '"', "\\n": "\n", "\\t": "\t"}
        return re.sub(r'\\[\\"nt]', lambda m: escapes[m.group(0)], raw[1:-1])
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    if raw in {"true", "false"}:
        return raw == "true"
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_parse_scalar(part) for part in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", inner) if part.strip()]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Unsupported TOML value: {raw}") from None


def _mini_toml(text: str) -> dict[str, Any]:
    """Small TOML subset for Python 3.10: tables, strings, numbers, booleans, flat arrays."""
    data: dict[str, Any] = {}
    table = data
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            table = data.setdefault(stripped[1:-1].strip(), {})
            continue
        if "=" not in stripped:
            raise ValueError(f"config line {number} is not key = value")
        key, value = stripped.split("=", 1)
        value = value.strip()
        if not value.startswith(("\"", "'")) and "#" in value:
            value = value.split("#", 1)[0].strip()
        elif value.startswith('"'):
            end = value.find('"', 1)
            while end > 0 and value[end - 1] == "\\":
                end = value.find('"', end + 1)
            value = value[:end + 1]
        table[key.strip()] = _parse_scalar(value)
    return data


def load_toml(path: Path) -> dict[str, Any]:
    if _toml is not None:
        with path.open("rb") as stream:
            return _toml.load(stream)
    return _mini_toml(path.read_text(encoding="utf-8"))


def is_openrouter(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").casefold()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _hhmm(value: str, label: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value))
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError(f"{label} must look like 08:00")
    return int(match[1]), int(match[2])


@dataclass(frozen=True)
class Settings:
    config_path: Path
    root: Path
    state_dir: Path
    mode: str = "approval"
    inbox: str = "00_INBOX/INBOX.md"
    single_actions: str = "01_GTD/SINGLE_ACTIONS.md"
    someday: str = "01_GTD/SOMEDAY_MAYBE.md"
    reviews_dir: str = "01_GTD/REVIEWS"
    projects_dir: str = "02_PROJECTS"
    areas_dir: str = "03_AREAS"
    reference_dir: str = "04_REFERENCE"
    archive_dir: str = "05_ARCHIVE"
    agent_dir: str = "_agent"
    review_template: str = "04_REFERENCE/SYSTEM/TEMPLATES/WEEKLY_REVIEW_TEMPLATE.md"
    poll_seconds: int = 10
    scan_interval_seconds: int = 60
    max_capture_chars: int = 6000
    timezone: str = "America/New_York"
    auto_promote_next: bool = True
    tidy_done_steps: bool = True
    edit_quiet_minutes: int = 10
    inbox_quiet_seconds: int = 30
    research_dir: str = "04_REFERENCE/RESEARCH"
    research_reports_dir: str = ""
    someday_topics: bool = True
    followup_context: str = "#computer"
    stale_action_days: int = 21
    untouched_project_days: int = 14
    review_weekday: int = 6
    review_hour: int = 8
    vague_check: bool = True
    calendar_enabled: bool = False
    calendar_source: str = "ics"          # "ics": a private link (read only); "folder": Radicale storage + CalDAV
    calendar_folder: str = ""
    calendar_user: str = ""
    calendar_name: str = ""
    calendar_url: str = "http://127.0.0.1:5232"
    calendar_alert_minutes: int = 10
    day_start: tuple[int, int] = (8, 0)
    day_end: tuple[int, int] = (22, 0)
    min_free_minutes: int = 15
    calendar_cache_minutes: int = 30
    telegram_enabled: bool = False
    telegram_chat_id: int = 0
    mail_enabled: bool = False
    mail_host: str = ""
    mail_port: int = 993
    mail_security: str = "tls"         # "tls" from the first byte, or "starttls"
    mail_ca_file: str = ""            # a certificate to trust for this connection only (Bridge's)
    mail_user: str = ""
    mail_source: str = "inbox"        # "inbox": the agent's own mailbox; "bridge": Proton through Bridge
    mail_folder: str = "INBOX"        # what the agent reads; Bridge: the GTD label, "Labels/GTD"
    mail_address: str = ""
    mail_own_addresses: tuple[str, ...] = ()
    mail_auth_server: str = ""
    mail_check_minutes: int = 5
    mail_private_senders: tuple[str, ...] = ()
    mail_private_words: tuple[str, ...] = ()
    mail_restricted_domains: tuple[str, ...] = ()
    jev_model: str = "typesafe/jev-1.13"
    glm_model: str = "z-ai/glm-5.3-flash"
    document_model: str = "z-ai/glm-5.3"
    documents_enabled: bool = True
    document_max_chars: int = 200_000
    deadline_lead_days: int = 3
    exam_lead_days: int = 4
    remote_inference: bool = False
    job_models: dict[str, str] = field(default_factory=dict)
    job_endpoints: dict[str, str] = field(default_factory=dict)
    job_keys: dict[str, str] = field(default_factory=dict)
    budget_monthly_usd: float = 5.0
    budget_warn_percent: int = 80
    trust_bookkeeping: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def job_model(self, job: str) -> str:
        return self.job_models.get(job) or (self.document_model if job == "documents" else self.glm_model)

    def job_endpoint(self, job: str) -> str:
        return self.job_endpoints.get(job) or self.job_endpoints.get("default") or OPENROUTER_CHAT

    def job_key_name(self, job: str) -> str | None:
        """The saved secret a job's endpoint uses: [endpoint_keys], else OpenRouter's key for OpenRouter, else none."""
        named = self.job_keys.get(job) or self.job_keys.get("default")
        if named:
            return named
        return "openrouter" if is_openrouter(self.job_endpoint(job)) else None

    @property
    def review(self) -> str:
        return f"{self.agent_dir}/APPROVAL.md"

    @property
    def inbox_dir(self) -> str:
        return self.inbox.rsplit("/", 1)[0] if "/" in self.inbox else ""

    @property
    def today_note(self) -> str:
        return f"{self.agent_dir}/TODAY.md"

    @property
    def lint_note(self) -> str:
        return f"{self.agent_dir}/LINT.md"

    @property
    def log_note(self) -> str:
        return f"{self.agent_dir}/LOG.md"

    @classmethod
    def load(cls, path: Path) -> "Settings":
        path = path.resolve()
        raw = load_toml(path)
        vault = raw.get("vault", {})
        agent = raw.get("agent", {})
        review = raw.get("review", {})
        calendar = raw.get("calendar", {})
        telegram = raw.get("telegram", {})
        mail = raw.get("mail", {})
        research = raw.get("research", {})
        models = raw.get("models", {})
        documents = raw.get("documents", {})
        privacy = raw.get("privacy", {})
        endpoints = {str(k): str(v).strip() for k, v in raw.get("endpoints", {}).items()}
        endpoint_keys = {str(k): str(v).strip() for k, v in raw.get("endpoint_keys", {}).items()}
        budget = raw.get("budget", {})
        trust = raw.get("trust", {})
        trusted = trust.get("bookkeeping", [])
        if not isinstance(trusted, list) or not all(isinstance(kind, str) for kind in trusted):
            raise ValueError("[trust] bookkeeping must be a list of proposal kinds, such as [\"follow_up\"]")
        for kind in trusted:
            if kind not in TRUSTABLE:
                raise ValueError(f"[trust] bookkeeping: unknown kind {kind}; use any of {', '.join(TRUSTABLE)}")
        for job, url in endpoints.items():
            if job not in (*MODEL_JOBS, "default"):
                raise ValueError(f"[endpoints] {job}: unknown job; use one of {', '.join(MODEL_JOBS)} or default")
            if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
                raise ValueError(f"[endpoints] {job} must be an https:// URL (or a local http://127.0.0.1 one)")
        if "root" not in vault:
            raise ValueError("config.toml needs [vault] root")
        if {"next_actions", "waiting_for", "review"} & set(vault):
            raise ValueError("config.toml is in the version 1 format (next_actions / waiting_for / review). "
                             "Start from config.example.toml; version 2 reads the vault layout in VAULT_RULES.md")
        root = Path(vault["root"]).expanduser().resolve()
        state = Path(agent.get("state_dir", "state"))
        if not state.is_absolute():
            state = path.parent / state
        state = state.resolve()
        if state == root or root in state.parents:
            raise ValueError("State directory must be outside the Obsidian vault")
        mode = agent.get("mode", "approval")
        if mode not in {"dry_run", "approval"}:
            raise ValueError("Only dry_run and approval modes are implemented")
        if calendar.get("source", "ics") not in {"ics", "folder"}:
            raise ValueError('[calendar] source must be "ics" (a private link) or "folder" (Radicale)')
        if calendar.get("source") == "folder" and calendar.get("enabled") and not (
                calendar.get("folder") and calendar.get("user")):
            raise ValueError("[calendar] source = \"folder\" needs folder (Radicale storage) and user")
        poll = int(agent.get("poll_seconds", 10))
        scan = int(agent.get("scan_interval_seconds", 60))
        if poll < 3 or scan < poll:
            raise ValueError("Scan interval must exceed the approval poll interval; poll must be at least 3 seconds")
        weekday = str(review.get("weekday", "sunday")).casefold()
        if weekday not in WEEKDAYS:
            raise ValueError("[review] weekday must be a weekday name")
        quiet = int(agent.get("edit_quiet_minutes", 10))
        if quiet < 0:
            raise ValueError("[agent] edit_quiet_minutes cannot be negative")
        inbox_quiet = int(agent.get("inbox_quiet_seconds", 30))
        if inbox_quiet < 0:
            raise ValueError("[agent] inbox_quiet_seconds cannot be negative")
        context = agent.get("followup_context", "#computer")
        if context not in CONTEXTS:
            raise ValueError("followup_context must be one of " + ", ".join(CONTEXTS))
        own = mail.get("own_addresses", [])
        if not isinstance(own, list) or not all(isinstance(address, str) for address in own):
            raise ValueError('[mail] own_addresses must be a list of your addresses, such as ["you@example.org"]')
        own_addresses = tuple(dict.fromkeys(address.strip().lower() for address in own if address.strip()))
        mail_port = int(mail.get("port", 993))
        source = str(mail.get("source", "inbox")).strip().lower()
        if source not in {"inbox", "bridge"}:
            raise ValueError('[mail] source must be "inbox" (the agent\'s own mailbox) or "bridge" (Proton)')
        folder = str(mail.get("folder", "Labels/GTD" if source == "bridge" else "INBOX")).strip()
        if source == "bridge" and (not folder.startswith("Labels/") or folder == "Labels/"):
            raise ValueError("[mail] folder: through Bridge the agent reads only a label, such as Labels/GTD, "
                             "never a folder")
        security = str(mail.get("security", "tls")).strip().lower()
        if security not in {"tls", "starttls"}:
            raise ValueError("[mail] security must be tls or starttls: the agent never logs in without TLS")
        if not 0 < mail_port < 65536:
            raise ValueError("[mail] port must be a port number, usually 993")
        if mail.get("enabled") and not (str(mail.get("host", "")).strip() and str(mail.get("user", "")).strip()
                                        and own_addresses):
            raise ValueError("[mail] enabled = true needs host, user and own_addresses")
        for name in ("private_senders", "private_words", "restricted_domains"):
            listed = mail.get(name, [])
            if not isinstance(listed, list) or not all(isinstance(item, str) for item in listed):
                raise ValueError(f"[mail] {name} must be a list of strings")
        restricted_domains = tuple(dict.fromkeys(
            domain.strip().lower() for domain in mail.get("restricted_domains", [])))
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", domain)
               for domain in restricted_domains):
            raise ValueError("[mail] restricted_domains must contain domain names, not addresses or URLs")
        address = str(mail.get("address", "")).strip().lower()
        if address and "@" not in address:
            raise ValueError("[mail] address must be an email address (this agent's own alias), or empty")
        mail_check = int(mail.get("check_minutes", 5))
        if mail_check < 1:
            raise ValueError("[mail] check_minutes must be at least 1")
        known = {"root", "inbox", "single_actions", "someday", "reviews_dir", "projects_dir", "areas_dir",
                 "reference_dir", "archive_dir", "agent_dir", "review_template"}
        paths = {key: str(vault[key]).strip("/") for key in known - {"root"} if key in vault}
        result = cls(
            config_path=path, root=root, state_dir=state, mode=mode, **paths,
            poll_seconds=poll, scan_interval_seconds=scan,
            max_capture_chars=int(agent.get("max_capture_chars", 6000)),
            timezone=str(agent.get("timezone", "America/New_York")),
            auto_promote_next=bool(agent.get("auto_promote_next", True)),
            tidy_done_steps=bool(agent.get("tidy_done_steps", True)),
            edit_quiet_minutes=quiet,
            inbox_quiet_seconds=inbox_quiet,
            research_reports_dir=str(research.get("reports_dir", "")).strip(),
            someday_topics=bool(review.get("someday_topics", True)),
            followup_context=context,
            stale_action_days=int(agent.get("stale_action_days", 21)),
            untouched_project_days=int(agent.get("untouched_project_days", 14)),
            review_weekday=WEEKDAYS.index(weekday), review_hour=int(review.get("hour", 8)),
            vague_check=bool(review.get("vague_check", True)),
            calendar_enabled=bool(calendar.get("enabled", False)),
            calendar_source=str(calendar.get("source", "ics")),
            calendar_folder=str(calendar.get("folder", "")).strip(),
            calendar_user=str(calendar.get("user", "")).strip(),
            calendar_name=str(calendar.get("calendar", "")).strip(),
            calendar_url=str(calendar.get("url", "http://127.0.0.1:5232")).strip().rstrip("/"),
            calendar_alert_minutes=int(calendar.get("alert_minutes", 10)),
            day_start=_hhmm(calendar.get("day_start", "08:00"), "day_start"),
            day_end=_hhmm(calendar.get("day_end", "22:00"), "day_end"),
            min_free_minutes=int(calendar.get("min_free_minutes", 15)),
            calendar_cache_minutes=int(calendar.get("cache_minutes", 30)),
            telegram_enabled=bool(telegram.get("enabled", False)),
            telegram_chat_id=int(telegram.get("chat_id", 0) or 0),
            mail_enabled=bool(mail.get("enabled", False)),
            mail_host=str(mail.get("host", "")).strip(),
            mail_port=mail_port,
            mail_security=security,
            mail_ca_file=str(mail.get("ca_file", "")).strip(),
            mail_user=str(mail.get("user", "")).strip(),
            mail_source=source,
            mail_folder=folder,
            mail_address=address,
            mail_own_addresses=own_addresses,
            mail_auth_server=str(mail.get("auth_server") or mail.get("host", "")).strip().lower(),
            mail_check_minutes=mail_check,
            mail_private_senders=tuple(s.strip().lower() for s in mail.get("private_senders", []) if s.strip()),
            mail_private_words=tuple(w.strip() for w in mail.get("private_words", []) if w.strip()),
            mail_restricted_domains=restricted_domains,
            jev_model=str(models.get("jev", "typesafe/jev-1.13")),
            glm_model=str(models.get("glm", "z-ai/glm-5.3-flash")),
            document_model=str(models.get("documents", "z-ai/glm-5.3")),
            documents_enabled=bool(documents.get("enabled", True)),
            document_max_chars=int(documents.get("max_chars", 200_000)),
            deadline_lead_days=int(documents.get("lead_days", 3)),
            exam_lead_days=int(documents.get("exam_lead_days", 4)),
            remote_inference=bool(privacy.get("remote_inference", False)),
            job_models={job: str(models[job]) for job in MODEL_JOBS if models.get(job)},
            job_endpoints=endpoints, job_keys=endpoint_keys,
            budget_monthly_usd=float(budget.get("monthly_usd", 5.0)),
            budget_warn_percent=int(budget.get("warn_percent", 80)),
            trust_bookkeeping=tuple(dict.fromkeys(trusted)),
        )
        for rel in (result.inbox, result.single_actions, result.someday, result.review):
            result.vault_path(rel)
        return result

    def vault_path(self, relative: str) -> Path:
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"Unsafe vault path: {relative}")
        target = (self.root / rel).resolve()
        if target == self.root or self.root not in target.parents:
            raise ValueError(f"Path escapes vault: {relative}")
        return target

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()


# ---------------------------------------------------------------- file I/O
@dataclass(frozen=True)
class Snapshot:
    path: Path
    text: str
    sha256: str | None
    newline: str


def read_snapshot(path: Path) -> Snapshot:
    data = path.read_bytes()
    text = data.decode("utf-8-sig")
    newline = "\r\n" if b"\r\n" in data else "\n"
    return Snapshot(path, text.replace("\r\n", "\n"), digest(data), newline)


def stable_snapshot(path: Path, delay: float = 0.15) -> Snapshot:
    first = read_snapshot(path)
    time.sleep(delay)
    second = read_snapshot(path)
    if first.sha256 != second.sha256:
        raise RuntimeError(f"File is changing: {path.name}")
    return second


def empty_snapshot(path: Path) -> Snapshot:
    return Snapshot(path, "", None, "\n")


def snapshot_or_empty(path: Path) -> Snapshot:
    return read_snapshot(path) if path.exists() else empty_snapshot(path)


def destination_hash(path: Path) -> str | None:
    return digest(path.read_bytes()) if path.exists() else None


def encode_text(text: str, newline: str) -> bytes:
    text = text.replace("\r\n", "\n")
    if newline != "\n":
        text = text.replace("\n", newline)
    return text.encode("utf-8")


def atomic_replace(path: Path, text: str, state_dir: Path, expected_hash: str | None,
                   newline: str = "\n", backup: bool = True) -> str:
    """Write text only if the file still has expected_hash (None: must not exist). Returns new hash."""
    current = destination_hash(path)
    if current != expected_hash:
        raise RuntimeError(f"File changed since it was read: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        backups = state_dir / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        shutil.copy2(path, backups / f"{path.name}.{stamp}.{uuid.uuid4().hex[:8]}.bak")
    payload = encode_text(text, newline)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Sync clients and editors can briefly lock files on Windows. Recheck the
        # hash on every attempt: a lock may hide a concurrent edit.
        delays = (0.1, 0.2, 0.4, 0.8, 1.6)
        for attempt in range(len(delays) + 1):
            try:
                if destination_hash(path) != expected_hash:
                    raise RuntimeError(f"File changed during write: {path.name}")
                os.replace(temporary, path)
                break
            except OSError as exc:
                if not (isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}):
                    raise
                if attempt == len(delays):
                    raise
                time.sleep(delays[attempt])
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            warnings.warn(f"Could not remove temporary write file: {temporary}", RuntimeWarning)
    return digest(payload)


def remove_file(path: Path, state_dir: Path, expected_hash: str) -> None:
    """Delete a file only if it still has expected_hash. A backup copy is kept in state/backups first."""
    if destination_hash(path) != expected_hash:
        raise RuntimeError(f"File changed since it was read: {path.name}")
    backups = state_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    shutil.copy2(path, backups / f"{path.name}.{stamp}.{uuid.uuid4().hex[:8]}.bak")
    delays = (0.1, 0.2, 0.4, 0.8, 1.6)
    for attempt in range(len(delays) + 1):
        try:
            if destination_hash(path) != expected_hash:
                raise RuntimeError(f"File changed during removal: {path.name}")
            os.unlink(path)
            return
        except OSError as exc:
            if not (isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}):
                raise
            if attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def move_file(source: Path, dest: Path, expected_hash: str) -> None:
    """Move a file (a PDF filed from the Inbox folder) only if it still has expected_hash, never over another file."""
    if destination_hash(source) != expected_hash:
        raise RuntimeError(f"File changed since it was read: {source.name}")
    if dest.exists():
        raise RuntimeError(f"{dest.name} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    delays = (0.1, 0.2, 0.4, 0.8, 1.6)
    for attempt in range(len(delays) + 1):
        try:
            os.replace(source, dest)
            return
        except OSError as exc:
            if not (isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}):
                raise
            if attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def write_owned(path: Path, text: str, state_dir: Path) -> bool:
    """Replace an agent-owned note if its content changed. Returns True when written."""
    current = read_snapshot(path) if path.exists() else None
    if current is not None and current.text == text.replace("\r\n", "\n"):
        return False
    atomic_replace(path, text, state_dir, current.sha256 if current else None,
                   current.newline if current else "\n", backup=False)
    return True


@contextmanager
def process_lock(state_dir: Path, name: str = "writer.lock") -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / name).open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(f"Another agent holds {name}; try again shortly") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(f"Another agent holds {name}; try again shortly") from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def single_writer(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(self, "_lock_held", False):
            return method(self, *args, **kwargs)
        with process_lock(self.settings.state_dir):
            self._lock_held = True
            try:
                return method(self, *args, **kwargs)
            finally:
                self._lock_held = False
    return wrapped


# ---------------------------------------------------------------- captures
CAPTURE_RE = re.compile(r"^\s*[-*+] \[([ xX])\]\s+(.+?)\s*$")
BULLET_RE = re.compile(r"^[-*+]\s+(.+)$")


@dataclass(frozen=True)
class Capture:
    capture_id: str
    text: str
    line_number: int
    checked: bool
    source_sha256: str


def parse_captures(snapshot: Snapshot, limit: int) -> list[Capture]:
    """Every nonblank Inbox line is one capture. Headings, fences and comments are skipped."""
    counts: dict[str, int] = {}
    found: list[Capture] = []
    in_fence = False
    in_comment = False
    for number, line in enumerate(snapshot.text.splitlines(), 1):
        stripped = line.strip()
        if in_comment:
            if "%%" in stripped:
                in_comment = False
            continue
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or re.match(r"^#{1,6}(?:\s|$)", stripped):
            continue
        if stripped in {"---", "***", "___"} or stripped.startswith("<!--"):
            continue
        if stripped.startswith("%%"):
            if stripped.count("%%") == 1:
                in_comment = True
            continue
        match = CAPTURE_RE.match(line)
        checked = match.group(1).lower() == "x" if match else False
        content = match.group(2).strip() if match else stripped
        if not match:
            if re.fullmatch(r"[-*+]\s+\[[ xX]\]", content):
                continue
            bullet = BULLET_RE.match(content)
            if bullet:
                content = bullet.group(1).strip()
        content = DONE_SUFFIX_RE.sub("", content).strip()
        if not content or len(content) > limit:
            continue
        counts[content] = counts.get(content, 0) + 1
        identity = digest(f"{content}\n{counts[content]}")[:24]
        found.append(Capture(identity, content, number, checked, digest(f"{identity}\n{content}\n{checked}")))
    return found


def remove_capture_line(snapshot: Snapshot, capture_id: str, source_hash: str, source_text: str,
                        limit: int) -> str | None:
    """Remove exactly one approved Inbox line; never delete a changed capture."""
    captures = parse_captures(snapshot, limit)
    capture = next((item for item in captures if item.capture_id == capture_id), None)
    if capture is None:
        if any(item.text == source_text for item in captures):
            raise RuntimeError("Inbox capture identity changed; manual cleanup required")
        return None
    if capture.source_sha256 != source_hash:
        raise RuntimeError("Inbox capture changed; manual cleanup required")
    lines = snapshot.text.splitlines(keepends=True)
    if not 1 <= capture.line_number <= len(lines):
        raise RuntimeError("Inbox line moved unexpectedly")
    del lines[capture.line_number - 1]
    return "".join(lines)


def append_capture_lines(snapshot: Snapshot, texts: list[str]) -> str:
    base = snapshot.text
    if base and not base.endswith("\n"):
        base += "\n"
    for text in texts:
        clean = " ".join(text.replace("\r", " ").split("\n")).strip()
        if clean:
            base += f"- {clean}\n"
    return base
