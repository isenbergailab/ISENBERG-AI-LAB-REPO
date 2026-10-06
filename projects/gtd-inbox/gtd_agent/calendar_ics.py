"""Read-only calendar from a private ICS link (for example Proton Calendar's share link).

Standard library only. Supports all-day and timed events, time zones, the common
RRULE forms (DAILY, WEEKLY with BYDAY, MONTHLY by day or weekday, YEARLY), COUNT,
UNTIL, EXDATE, RECURRENCE-ID overrides and cancelled events.
"""
from __future__ import annotations

import calendar
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

WEEKDAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


@dataclass(frozen=True)
class Event:
    summary: str
    start: datetime
    end: datetime
    all_day: bool
    busy: bool
    uid: str = ""
    notes: str = ""  # DESCRIPTION, one line


# ---------------------------------------------------------------- fetch
def fetch_ics(url: str, cache: Path, max_age_minutes: int) -> tuple[str | None, str | None]:
    """Return (ics text, warning). Uses a cached copy when fresh or when the network fails."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    fresh = cache.exists() and time.time() - cache.stat().st_mtime < max_age_minutes * 60
    if fresh:
        return cache.read_text(encoding="utf-8"), None
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "gtd-agent/2"})
        with urllib.request.urlopen(request, timeout=20) as response:
            text = response.read().decode("utf-8", errors="replace")
        if "BEGIN:VCALENDAR" not in text:
            raise ValueError("not an ICS calendar")
        cache.write_text(text, encoding="utf-8")
        return text, None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        if cache.exists():
            return cache.read_text(encoding="utf-8"), f"Calendar refresh failed ({type(exc).__name__}); showing the last copy"
        return None, f"Calendar unavailable ({type(exc).__name__})"


# ---------------------------------------------------------------- parse
def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_property(line: str) -> tuple[str, dict[str, str], str]:
    in_quote = False
    for index, char in enumerate(line):
        if char == '"':
            in_quote = not in_quote
        elif char == ":" and not in_quote:
            head, value = line[:index], line[index + 1:]
            break
    else:
        return line.upper(), {}, ""
    parts = head.split(";")
    params = {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        params[key.upper()] = val.strip('"')
    return parts[0].upper(), params, value


def _unescape(value: str) -> str:
    return value.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _to_local(value: str, params: dict[str, str], local_zone: str) -> tuple[datetime, bool]:
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        day = datetime.strptime(value[:8], "%Y%m%d")
        return day, True
    utc = value.endswith("Z")
    stamp = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        return stamp.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None), False
    tzid = params.get("TZID")
    if tzid and tzid != local_zone:
        try:
            from zoneinfo import ZoneInfo
            return stamp.replace(tzinfo=ZoneInfo(tzid)).astimezone().replace(tzinfo=None), False
        except Exception:  # no tz database (Windows without tzdata): treat as local time
            return stamp, False
    return stamp, False


def _duration(value: str) -> timedelta:
    match = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not match:
        return timedelta(0)
    sign = -1 if match.group(1) == "-" else 1
    weeks, days, hours, minutes, seconds = (int(g or 0) for g in match.groups()[1:])
    return sign * timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)


def _rrule(value: str) -> dict[str, str]:
    return {k.upper(): v for k, _, v in (part.partition("=") for part in value.split(";") if part)}


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date | None:
    days = [d for d in range(1, calendar.monthrange(year, month)[1] + 1) if date(year, month, d).weekday() == weekday]
    try:
        return date(year, month, days[nth - 1 if nth > 0 else nth])
    except IndexError:
        return None


def _occurrences(start: datetime, rule: dict[str, str], range_end: datetime, local_zone: str) -> list[datetime]:
    freq = rule.get("FREQ", "")
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT") else None
    until = None
    if rule.get("UNTIL"):
        until, until_is_date = _to_local(rule["UNTIL"], {}, local_zone)
        if until_is_date:
            until += timedelta(days=1, seconds=-1)
    byday = [d.strip() for d in rule.get("BYDAY", "").split(",") if d.strip()]
    bymonthday = [int(d) for d in rule.get("BYMONTHDAY", "").split(",") if d.strip().lstrip("-").isdigit()]
    results: list[datetime] = []
    clock = start.time()

    def emit(candidate: datetime) -> bool:
        if candidate < start:
            return True
        if until is not None and candidate > until:
            return False
        if count is not None and len(results) >= count:
            return False
        if candidate > range_end and count is None:
            return False
        results.append(candidate)
        return True

    guard = 0
    if freq == "DAILY":
        current = start
        while guard < 5000 and emit(current):
            current += timedelta(days=interval)
            guard += 1
    elif freq == "WEEKLY":
        weekdays = sorted(WEEKDAY_CODES.index(code[-2:]) for code in byday) or [start.weekday()]
        week = start - timedelta(days=start.weekday())
        going = True
        while going and guard < 5000:
            for weekday in weekdays:
                candidate = datetime.combine((week + timedelta(days=weekday)).date(), clock)
                if not emit(candidate):
                    going = False
                    break
            week += timedelta(weeks=interval)
            guard += 1
    elif freq == "MONTHLY":
        year, month = start.year, start.month
        going = True
        while going and guard < 2000:
            days: list[date] = []
            if byday:
                for code in byday:
                    match = re.fullmatch(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", code)
                    if not match:
                        continue
                    nth = int(match.group(1) or 1)
                    found = _nth_weekday(year, month, WEEKDAY_CODES.index(match.group(2)), nth)
                    if found:
                        days.append(found)
            else:
                last = calendar.monthrange(year, month)[1]
                for day in bymonthday or [start.day]:
                    real = day if day > 0 else last + day + 1
                    if 1 <= real <= last:
                        days.append(date(year, month, real))
            for day in sorted(days):
                if not emit(datetime.combine(day, clock)):
                    going = False
                    break
            month += interval
            while month > 12:
                month -= 12
                year += 1
            guard += 1
    elif freq == "YEARLY":
        year = start.year
        while guard < 200:
            try:
                candidate = start.replace(year=year)
            except ValueError:  # 29 February
                year += interval
                guard += 1
                continue
            if not emit(candidate):
                break
            year += interval
            guard += 1
    else:
        results.append(start)
    return results


def _version(event: dict) -> tuple[int, str]:
    """Which copy of an event is newer: SEQUENCE first, then LAST-MODIFIED (or DTSTAMP)."""
    try:
        sequence = int(str(event.get("SEQUENCE") or 0).strip())
    except ValueError:
        sequence = 0
    return sequence, str(event.get("LAST-MODIFIED") or event.get("DTSTAMP") or "")


def parse_events(text: str, range_start: datetime, range_end: datetime,
                 local_zone: str = "America/New_York") -> list[Event]:
    raw_events: list[dict] = []
    current: dict | None = None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            current = {"EXDATE": []}
            continue
        if line == "END:VEVENT":
            if current is not None:
                raw_events.append(current)
            current = None
            continue
        if current is None or not line:
            continue
        name, params, value = _split_property(line)
        if name == "EXDATE":
            for part in value.split(","):
                current["EXDATE"].append(_to_local(part, params, local_zone)[0])
        elif name in {"DTSTART", "DTEND", "RECURRENCE-ID"}:
            current[name] = _to_local(value, params, local_zone)
        else:
            current[name] = value
    # A calendar holds each event once: its UID, plus RECURRENCE-ID for one changed instance. A feed that lists an
    # event more than once (Proton's share link repeats some) counts it once, and the newest copy stands.
    masters: dict[str, dict] = {}
    overrides: dict[tuple[str, datetime], dict] = {}
    for event in raw_events:
        uid = event.get("UID")
        if not uid:
            continue
        if "RECURRENCE-ID" in event:
            key = (uid, event["RECURRENCE-ID"][0])
            if key not in overrides or _version(event) >= _version(overrides[key]):
                overrides[key] = event
        elif uid not in masters or _version(event) >= _version(masters[uid]):
            masters[uid] = event
    events: list[Event] = []

    def add(event: dict, start: datetime, all_day: bool, length: timedelta) -> None:
        if event.get("STATUS", "").upper() == "CANCELLED":
            return
        end = start + length
        if end <= range_start or start >= range_end:
            return
        busy = event.get("TRANSP", "OPAQUE").upper() != "TRANSPARENT" and not all_day
        events.append(Event(_unescape(event.get("SUMMARY", "(busy)")) or "(busy)", start, end, all_day, busy,
                            event.get("UID", ""), _unescape(event.get("DESCRIPTION", ""))))

    for event in raw_events:
        if "DTSTART" not in event or "RECURRENCE-ID" in event:
            continue
        if event.get("UID") and masters.get(event["UID"]) is not event:
            continue  # a repeated copy
        start, all_day = event["DTSTART"]
        if "DTEND" in event:
            length = event["DTEND"][0] - start
        elif "DURATION" in event:
            length = _duration(event["DURATION"])
        else:
            length = timedelta(days=1) if all_day else timedelta(0)
        if length.total_seconds() < 0:
            length = timedelta(0)
        if "RRULE" in event:
            starts = _occurrences(start, _rrule(event["RRULE"]), range_end, local_zone)
        else:
            starts = [start]
        excluded = set(event["EXDATE"])
        for occurrence in starts:
            if occurrence in excluded:
                continue
            override = overrides.get((event.get("UID", ""), occurrence))
            if override is not None:
                if "DTSTART" in override:
                    o_start, o_all_day = override["DTSTART"]
                    o_length = (override["DTEND"][0] - o_start) if "DTEND" in override else length
                    add(override, o_start, o_all_day, o_length)
                continue
            add(event, occurrence, all_day, length)
    events.sort(key=lambda e: (e.start, e.summary))
    return events


@dataclass(frozen=True)
class Invite:
    """One event as an invitation (.ics) sends it, for an action to add it by hand."""
    summary: str
    start: datetime
    end: datetime
    all_day: bool
    location: str
    repeats: bool


def invites(text: str, local_zone: str = "") -> list[Invite]:
    """The events an invitation offers. A cancellation, and cancelled events, offer none; overrides of single
    repeats are left out (the repeating event stands for them)."""
    found: list[Invite] = []
    current: dict | None = None
    cancel = False
    for line in _unfold(text):
        if current is None and line.upper().startswith("METHOD:") and line.split(":", 1)[1].strip().upper() == "CANCEL":
            cancel = True
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None and "DTSTART" in current and "RECURRENCE-ID" not in current \
                    and current.get("STATUS", "").upper() != "CANCELLED":
                start, all_day = current["DTSTART"]
                if "DTEND" in current:
                    end = current["DTEND"][0]
                elif "DURATION" in current:
                    end = start + _duration(current["DURATION"])
                else:
                    end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
                found.append(Invite(_unescape(current.get("SUMMARY", "")).strip() or "Event", start, max(end, start),
                                    all_day, _unescape(current.get("LOCATION", "")).strip(), "RRULE" in current))
            current = None
            continue
        if current is None or not line:
            continue
        name, params, value = _split_property(line)
        if name in {"DTSTART", "DTEND"}:
            try:
                current[name] = _to_local(value, params, local_zone)
            except ValueError:
                continue
        else:
            current[name] = value
    return [] if cancel else found


def free_gaps(events: list[Event], day: date, start: tuple[int, int], end: tuple[int, int],
              now: datetime | None, minimum: int) -> list[tuple[datetime, datetime]]:
    window_start = datetime.combine(day, datetime.min.time()).replace(hour=start[0], minute=start[1])
    window_end = datetime.combine(day, datetime.min.time()).replace(hour=end[0], minute=end[1])
    if now is not None and now.date() == day:
        window_start = max(window_start, now.replace(second=0, microsecond=0))
    busy = sorted((max(e.start, window_start), min(e.end, window_end)) for e in events
                  if e.busy and e.end > window_start and e.start < window_end)
    gaps = []
    cursor = window_start
    for begin, finish in busy:
        if begin > cursor and (begin - cursor).total_seconds() >= minimum * 60:
            gaps.append((cursor, begin))
        cursor = max(cursor, finish)
    if window_end > cursor and (window_end - cursor).total_seconds() >= minimum * 60:
        gaps.append((cursor, window_end))
    return gaps


# ---------------------------------------------------------------- write
# US Eastern rules, as DAVx5 writes them. Events carry local times with this zone, so a repeating event keeps
# its clock time across daylight saving, and no time-zone database is needed on Windows.
VTIMEZONES = {"America/New_York": (
    "BEGIN:VTIMEZONE", "TZID:America/New_York",
    "BEGIN:STANDARD", "DTSTART:20071104T020000", "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU", "TZNAME:EST",
    "TZOFFSETFROM:-0400", "TZOFFSETTO:-0500", "END:STANDARD",
    "BEGIN:DAYLIGHT", "DTSTART:20070311T020000", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU", "TZNAME:EDT",
    "TZOFFSETFROM:-0500", "TZOFFSETTO:-0400", "END:DAYLIGHT",
    "END:VTIMEZONE")}


def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """Fold at 75 octets, as RFC 5545 asks, without splitting a UTF-8 character."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    parts, current, size = [], "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > (75 if not parts else 74):
            parts.append(current)
            current, size = "", 0
        current += char
        size += width
    parts.append(current)
    return "\r\n ".join(parts)


def event_ics(uid: str, summary: str, start: datetime, end: datetime, *, zone: str, alert_minutes: int,
              stamp: datetime, rrule: str | None = None) -> str:
    """One VEVENT with a phone alert. Local times carry TZID and its VTIMEZONE when the zone is known;
    otherwise they are floating (the phone shows them in its own zone)."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Obsidian GTD agent//EN", "CALSCALE:GREGORIAN"]
    timezone_block = VTIMEZONES.get(zone)
    if timezone_block:
        lines += timezone_block
    prefix = f";TZID={zone}" if timezone_block else ""
    trigger = f"-PT{alert_minutes}M" if alert_minutes > 0 else "PT0M"
    lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp:%Y%m%dT%H%M%S}Z",
              f"DTSTART{prefix}:{start:%Y%m%dT%H%M%S}", f"DTEND{prefix}:{end:%Y%m%dT%H%M%S}"]
    if rrule:
        lines.append(f"RRULE:{rrule}")
    lines += [f"SUMMARY:{_escape(summary)}", "STATUS:CONFIRMED", "BEGIN:VALARM", "ACTION:DISPLAY",
              f"DESCRIPTION:{_escape(summary)}", f"TRIGGER:{trigger}", "END:VALARM", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


_DAY_CODES = {name.casefold(): code for name, code in zip(calendar.day_name, WEEKDAY_CODES)}
_MONTHS = {name.casefold(): number for number, name in enumerate(calendar.month_name) if name}


def _days(text: str) -> list[str] | None:
    names = [part for part in re.split(r"\s*(?:,|\band\b)\s*", text.strip()) if part]
    codes = [_DAY_CODES.get(name) for name in names]
    return None if not codes or None in codes else codes  # type: ignore[return-value]


def _month_day(text: str) -> int | None:
    if text == "the last":
        return -1
    match = re.fullmatch(r"the (\d{1,2})(?:st|nd|rd|th)", text)
    return int(match[1]) if match and 1 <= int(match[1]) <= 31 else None


def rrule_from_tasks(rule: str, due: date | None) -> str | None:
    """The calendar rule for a Tasks repeat ('every week on Monday' -> FREQ=WEEKLY;BYDAY=MO).
    Rules the calendar cannot mirror ('when done', nth weekdays, blue moons) give None."""
    text = " ".join(rule.casefold().split())
    if "when done" in text or not text.startswith("every "):
        return None
    text = text[len("every "):]
    if text == "weekday":
        return "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    days = _days(text)
    if days:
        return "FREQ=WEEKLY;BYDAY=" + ",".join(days)
    match = re.fullmatch(r"(?:(\d{1,2}) )?(day|week|month|year)s?(?: on (.+))?", text)
    if match:
        count, unit, on = int(match[1] or 1), match[2], match[3]
        parts = [f"FREQ={ {'day': 'DAILY', 'week': 'WEEKLY', 'month': 'MONTHLY', 'year': 'YEARLY'}[unit] }"]
        if count > 1:
            parts.append(f"INTERVAL={count}")
        if unit == "day":
            return None if on else ";".join(parts)
        if unit == "week":
            codes = _days(on) if on else ([WEEKDAY_CODES[due.weekday()]] if due else None)
            return ";".join(parts + [f"BYDAY={','.join(codes)}"]) if codes else None
        if unit == "month":
            day = _month_day(on) if on else (due.day if due else None)
            return ";".join(parts + [f"BYMONTHDAY={day}"]) if day else None
        return None if on else ";".join(parts)
    match = re.fullmatch(r"([a-z]+) on (.+)", text)
    if match and match[1] in _MONTHS:
        day = _month_day(match[2])
        return f"FREQ=YEARLY;BYMONTH={_MONTHS[match[1]]};BYMONTHDAY={day}" if day and day > 0 else None
    return None
