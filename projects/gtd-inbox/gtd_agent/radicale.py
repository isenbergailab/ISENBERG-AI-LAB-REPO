"""the user's Radicale calendar. Events are read straight from Radicale's storage folder (a synced folder), and events
the agent adds after approval go in over CalDAV, so Radicale validates them, keeps its own lock, cache and sync
tokens, and the phone syncs them the normal way (Radicale docs: external writes need its storage lock, which
cannot be tested from the Linux build VM).

Storage layout: <folder>/collection-root/<user>/<collection>/ holds one .ics file per item plus .Radicale.props
(JSON: displayname, supported components). .Radicale.cache holds Radicale's own cache and is never read.
"""
from __future__ import annotations

import base64
import http.client
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


class CalendarError(RuntimeError):
    """The calendar could not be read or changed. Plain words; the agent retries later."""


@dataclass(frozen=True)
class Collection:
    id: str
    path: Path
    displayname: str
    components: tuple[str, ...]


def collections(folder: Path, user: str) -> list[Collection]:
    """The user's calendar collections (VEVENT-capable), sorted by display name."""
    base = folder / "collection-root" / user
    if not base.is_dir():
        raise CalendarError(f"Radicale calendar folder not found: {base}")
    found = []
    for path in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
        try:
            props = json.loads((path / ".Radicale.props").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if props.get("tag") != "VCALENDAR":
            continue
        components = tuple(c.strip() for c in str(props.get("C:supported-calendar-component-set") or "VEVENT").split(",")
                           if c.strip())
        if "VEVENT" not in components:
            continue
        found.append(Collection(path.name, path, str(props.get("D:displayname") or path.name), components))
    return sorted(found, key=lambda c: c.displayname.casefold())


def choose(found: list[Collection], name: str) -> Collection:
    """The calendar named in config (display name or folder id), or the only one there is."""
    if name:
        hits = [c for c in found if name in (c.id, c.displayname)]
        if len(hits) == 1:
            return hits[0]
        raise CalendarError(f"No single Radicale calendar is named '{name}' "
                            f"(found: {', '.join(c.displayname for c in found) or 'none'})")
    if len(found) == 1:
        return found[0]
    raise CalendarError("Several Radicale calendars exist; set [calendar] calendar to one of: "
                        + ", ".join(c.displayname for c in found))


def read_items(collection: Collection) -> str:
    """Every item file in the collection, concatenated. Radicale's cache folder is skipped."""
    texts = []
    for path in sorted(collection.path.glob("*.ics")):
        if path.name.startswith("."):
            continue
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue  # a sync tool may hold a file for a moment; the next scan reads it
    return "\n".join(texts)


def _blocks(text: str, name: str) -> list[str]:
    """Each BEGIN:name ... END:name block, lines kept as stored (folding included)."""
    found, current = [], None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line == f"BEGIN:{name}":
            current = [line]
        elif current is not None:
            current.append(line)
            if line == f"END:{name}":
                found.append("\r\n".join(current))
                current = None
    return found


def export(folder: Path, user: str, out_dir: Path) -> list[tuple[Path, int]]:
    """Each Radicale calendar as one .ics file to import elsewhere (Proton Calendar › Settings › Import/export ›
    Import from ICS): every event as stored, alerts and repeats included, with each time zone they name once.
    (file, events) per calendar."""
    written = []
    for collection in collections(folder, user):
        zones: dict[str, str] = {}
        events: list[str] = []
        for path in sorted(collection.path.glob("*.ics")):
            if path.name.startswith("."):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for zone in _blocks(text, "VTIMEZONE"):
                match = re.search(r"^TZID[^:]*:(.+)$", zone, re.MULTILINE)
                zones.setdefault(match.group(1).strip() if match else zone, zone)
            events += _blocks(text, "VEVENT")
        if not events:
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.-]+", "_", collection.displayname).strip("_") or collection.id
        out = out_dir / f"radicale-{name}.ics"
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//GTD agent//Radicale export//EN", "CALSCALE:GREGORIAN",
                 *zones.values(), *events, "END:VCALENDAR"]
        out.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
        written.append((out, len(events)))
    return written


class CalDAV:
    """Add and remove single events in one Radicale calendar over CalDAV (127.0.0.1 by default)."""

    def __init__(self, base_url: str, user: str, password: str | None, collection_id: str, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.collection_id = collection_id
        self.timeout = timeout

    def href(self, uid: str) -> str:
        return f"{self.base_url}/{quote(self.user)}/{quote(self.collection_id)}/{quote(uid)}.ics"

    def _request(self, method: str, url: str, body: bytes | None = None,
                 headers: dict[str, str] | None = None) -> tuple[int, str | None]:
        if not self.password:
            raise CalendarError("No Radicale password is saved. Run: run.ps1 set-secret radicale")
        token = base64.b64encode(f"{self.user}:{self.password}".encode("utf-8")).decode("ascii")
        request = urllib.request.Request(url, data=body, method=method,
                                         headers={"Authorization": f"Basic {token}", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
                return response.status, response.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            exc.close()  # the status and headers say enough; closing it frees the connection
            if exc.code == 401:
                raise CalendarError("Radicale refused the password. Run: run.ps1 set-secret radicale") from exc
            return exc.code, exc.headers.get("ETag") if exc.headers else None
        except (OSError, http.client.HTTPException) as exc:
            raise CalendarError(f"Radicale is not answering at {self.base_url} ({type(exc).__name__}). "
                                "It starts at sign-in; the change is tried again shortly") from exc

    def add(self, uid: str, ics: str) -> dict[str, str]:
        url = self.href(uid)
        status, etag = self._request("PUT", url, ics.encode("utf-8"),
                                     {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
        if status == 412:
            raise CalendarError("The calendar already holds an event with this id")
        if status not in (200, 201, 204):
            raise CalendarError(f"Radicale did not take the event (HTTP {status})")
        if not etag:
            status, etag = self._request("GET", url)
        return {"href": url, "etag": etag or ""}

    def remove(self, href: str, etag: str) -> str:
        """'deleted', 'missing' (already gone) or 'changed' (edited elsewhere since; left alone)."""
        headers = {"If-Match": etag} if etag else {}
        status, _ = self._request("DELETE", href, headers=headers)
        if status in (200, 204):
            return "deleted"
        if status == 404:
            return "missing"
        if status == 412:
            return "changed"
        raise CalendarError(f"Radicale did not remove the event (HTTP {status})")

    def exists(self, href: str) -> bool:
        status, _ = self._request("GET", href)
        return status == 200
