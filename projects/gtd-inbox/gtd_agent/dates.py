"""Resolve a small, explicit set of GTD deadline phrases in local Python."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


@dataclass(frozen=True)
class Deadline:
    phrase: str
    day: date


_PHRASES = re.compile(
    r"(?i)\b(?:end\s+of\s+(?:the\s+)?week|end\s+of\s+(?:the\s+)?month|"
    r"this\s+friday|next\s+week|next\s+month|tomorrow|eow|eom)\b"
)
_DUE_PREFIX = re.compile(r"(?i)\b(?:by|due(?:\s+on)?|deadline(?:\s+is)?)\s*$")


def _add_month(day: date) -> date:
    return _add_months(day, 1)


def _add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


_COUNT_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "eighteen": 18, "couple of": 2,
                "few": 3}
_SPAN_RE = re.compile(r"(?:in\s+|about\s+|around\s+)*(\d{1,3}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|"
                      r"eleven|twelve|eighteen|a couple of|a few)\s+(day|week|month|year)s?"
                      r"(?:\s+from\s+(?:now|today)|\s+later)?")


def resolve_span(text: str, reference: date) -> date | None:
    """Longer relative spans for when to look at something again: 'in a year', '6 months from now', 'next year'."""
    text = text.strip().casefold()
    match = _SPAN_RE.fullmatch(text)
    if match:
        word = match[1].removeprefix("a ") if match[1].startswith("a ") and match[1] != "a" else match[1]
        count = int(word) if word.isdigit() else _COUNT_WORDS[word]
        unit = match[2]
        if unit == "day":
            return reference + timedelta(days=count)
        if unit == "week":
            return reference + timedelta(weeks=count)
        return _add_months(reference, count * (12 if unit == "year" else 1))
    if text in {"next year", "early next year"}:
        return date(reference.year + 1, 1, 1)
    if text in {"end of the year", "end of year", "by the end of the year", "eoy"}:
        return date(reference.year, 12, 31)
    months = {name.casefold(): i for i, name in enumerate(calendar.month_name) if name}
    match = re.fullmatch(r"(?:in\s+|next\s+|early\s+)?([a-z]+)(?:\s+(\d{4}))?", text)
    if match and match[1] in months:
        month = months[match[1]]
        year = int(match[2]) if match[2] else reference.year + (month < reference.month)
        return date(year, month, 1)
    return None


def resolve_deadline(text: str, reference: date, *, require_due_marker: bool = False) -> Deadline | None:
    """EoW means Friday; next week means seven days; next month clamps day."""
    matches = []
    for match in _PHRASES.finditer(text):
        if require_due_marker and not _DUE_PREFIX.search(text[max(0, match.start() - 24):match.start()]):
            continue
        matches.append(match)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Several deadline phrases found; choose one")
    phrase = matches[0].group().casefold()
    if phrase == "tomorrow":
        day = reference + timedelta(days=1)
    elif phrase == "next week":
        day = reference + timedelta(days=7)
    elif phrase == "next month":
        day = _add_month(reference)
    elif phrase in {"eom", "end of month", "end of the month"}:
        day = date(reference.year, reference.month,
                   calendar.monthrange(reference.year, reference.month)[1])
    else:
        day = reference + timedelta(days=(4 - reference.weekday()) % 7)
    return Deadline(matches[0].group(), day)


_CLOCK_RE = re.compile(r"(?:at\s+|from\s+|by\s+|starting\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?")
TIME_IN_TEXT_RE = re.compile(r"(?i)(?<![\d:])(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)(?![a-z])|(?:[01]?\d|2[0-3]):[0-5]\d)")


def resolve_time_evidence(evidence: str) -> time | None:
    """A clock time: '3pm', '3:30 pm', '15:30', 'noon'. A bare '3' is ambiguous and fails closed."""
    text = evidence.strip().casefold()
    if not text:
        return None
    if text in {"noon", "midday", "at noon", "12 noon"}:
        return time(12, 0)
    if text in {"midnight", "at midnight"}:
        return time(0, 0)
    match = _CLOCK_RE.fullmatch(text)
    if not match:
        raise ValueError("Unsupported time; write it like 3pm or 15:30")
    hour, minute, meridiem = int(match[1]), int(match[2] or 0), match[3]
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError("An am/pm hour runs from 1 to 12")
        hour = hour % 12 + (12 if meridiem.startswith("p") else 0)
    elif match[2] is None:
        raise ValueError("Say am or pm, or use a 24-hour time such as 15:30")
    if hour > 23 or minute > 59:
        raise ValueError("Unsupported time; write it like 3pm or 15:30")
    return time(hour, minute)


_DURATION_RE = re.compile(r"(?:for\s+)?(?:(an?|one|1|2|3|4|5|6|two|three|half an?)\s*(hours?|hrs?|h)|"
                          r"(\d{1,3})\s*(minutes?|mins?|m)|(\d(?:\.\d)?)\s*(hours?|hrs?|h)|an hour and a half)")


def resolve_end_evidence(evidence: str, start: datetime) -> datetime | None:
    """When a timed event ends: a duration ('for an hour', '90 minutes') or an end time ('until 4', '-4:30pm')."""
    text = evidence.strip().casefold().lstrip("-–— ").strip()
    if not text:
        return None
    match = _DURATION_RE.fullmatch(text)
    if match:
        if text == "an hour and a half":
            return start + timedelta(minutes=90)
        if match[1]:
            word = match[1]
            hours = 0.5 if word.startswith("half") else _COUNT_WORDS.get(word, None) or int(word)
            return start + timedelta(minutes=int(hours * 60))
        if match[3]:
            return start + timedelta(minutes=int(match[3]))
        return start + timedelta(minutes=int(float(match[5]) * 60))
    text = re.sub(r"^(?:until|till|til|to|through)\s+", "", text)
    match = _CLOCK_RE.fullmatch(text)
    if not match:
        raise ValueError("Unsupported end; write it like 'until 4pm' or 'for an hour'")
    if match[3]:
        clock = resolve_time_evidence(text)
    else:  # '4' or '4:30' after a 3pm start: the first such time after the start
        hour, minute = int(match[1]), int(match[2] or 0)
        if hour > 23 or minute > 59:
            raise ValueError("Unsupported end time")
        options = [time(h, minute) for h in (hour, hour + 12) if h <= 23]
        later = [c for c in options if datetime.combine(start.date(), c) > start]
        if not later:
            raise ValueError("The end time comes before the start")
        clock = later[0]
    end = datetime.combine(start.date(), clock)
    if end <= start:
        raise ValueError("The end time comes before the start")
    return end


def find_time(text: str) -> time | None:
    """The first clock time written in a task's words, such as 'Write board agenda (by 1:30pm)'."""
    match = TIME_IN_TEXT_RE.search(text)
    if not match:
        return None
    try:
        return resolve_time_evidence(match[1])
    except ValueError:
        return None


def resolve_date_evidence(evidence: str, reference: date) -> date | None:
    """Calculate ONLY the date span selected by GLM, never the whole capture.

    Missing years use the capture year; dates never silently roll into next year.
    Unsupported or conflicting spans fail closed for teacher feedback.
    """
    text = evidence.strip().casefold()
    if not text:
        return None
    if text in {"today", "yesterday", "tomorrow"}:
        return reference + timedelta(days={"today": 0, "yesterday": -1, "tomorrow": 1}[text])
    relative = resolve_deadline(text, reference)
    if relative and relative.phrase.casefold() == text:
        return relative.day
    weekdays = {name.casefold(): i for i, name in enumerate(calendar.day_name)}
    match = re.fullmatch(r"(?:(this|next)\s+)?(" + "|".join(weekdays) + r")", text)
    if match:
        delta = (weekdays[match[2]] - reference.weekday()) % 7
        if match[1] == "next" and delta == 0:
            delta = 7
        return reference + timedelta(days=delta)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)
    span = resolve_span(text, reference)
    if span is not None:
        return span
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", text)
    if match:
        return date(int(match[3] or reference.year), int(match[1]), int(match[2]))
    months = {name.casefold(): i for i, name in enumerate(calendar.month_name) if name}
    months.update({name.casefold(): i for i, name in enumerate(calendar.month_abbr) if name})
    match = re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", text)
    if match and match[1] in months:
        return date(int(match[3] or reference.year), months[match[1]], int(match[2]))
    raise ValueError("Unsupported or ambiguous date span; supply a single date such as 2026-09-30")
