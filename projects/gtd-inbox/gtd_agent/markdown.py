"""Small, dependency-free Markdown helpers: frontmatter, headings, sections, wikilinks."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LINK_RE = re.compile(r"(!?)\[\[([^\]\n]+?)\]\]")
CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


# ---------------------------------------------------------------- frontmatter
def split_frontmatter(text: str) -> tuple[str | None, int]:
    """Return (frontmatter text, index of first body line)."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, 0
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            return "\n".join(lines[1:index]), index + 1
    return None, 0


def _scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    if value.casefold() in {"true", "yes"}:
        return True
    if value.casefold() in {"false", "no"}:
        return False
    if value in {"null", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        items, current, depth, quote = [], "", 0, ""
        for char in inner:
            if quote:
                current += char
                if char == quote:
                    quote = ""
                continue
            if char in "\"'":
                quote = char
                current += char
            elif char == "[":
                depth += 1
                current += char
            elif char == "]":
                depth -= 1
                current += char
            elif char == "," and depth == 0:
                items.append(_scalar(current))
                current = ""
            else:
                current += char
        if current.strip():
            items.append(_scalar(current))
        return items
    return value


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse the simple YAML used in this vault: scalars, inline lists and block lists."""
    raw, _ = split_frontmatter(text)
    data: dict[str, Any] = {}
    if raw is None:
        return data
    key = None
    for line in raw.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s*(.*)$", line) or re.match(r"^-\s+(.*)$", line)
        if item and key is not None:
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key].append(_scalar(item.group(1)))
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$", line)
        if not match:
            continue
        key = match.group(1)
        value = match.group(2)
        # Strip trailing comments outside quotes.
        if not value.strip().startswith(("\"", "'")) and " #" in value:
            value = value.split(" #", 1)[0]
        data[key] = _scalar(value)
    return data


def link_target(value: Any) -> str | None:
    """'[[AREA_X]]', '[[AREA_X|Alias]]' or 'AREA_X' -> 'AREA_X'."""
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    text = str(value).strip()
    match = re.fullmatch(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]", text)
    if match:
        return match.group(1).strip()
    return text or None


# ---------------------------------------------------------------- lines
@dataclass(frozen=True)
class LineInfo:
    index: int          # 0-based line index
    text: str
    in_code: bool
    heading_level: int  # 0 if not a heading
    heading: str        # heading text for heading lines
    section: str        # nearest level-2 heading above (or "")
    nearest: str        # nearest heading of any level above (or "")


def walk(text: str) -> Iterator[LineInfo]:
    _, body_start = split_frontmatter(text)
    in_code = False
    section = ""
    nearest = ""
    for index, line in enumerate(text.split("\n")):
        if index < body_start:
            yield LineInfo(index, line, True, 0, "", "", "")
            continue
        if FENCE_RE.match(line):
            in_code = not in_code
            yield LineInfo(index, line, True, 0, "", section, nearest)
            continue
        if in_code:
            yield LineInfo(index, line, True, 0, "", section, nearest)
            continue
        match = HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            if level <= 2:
                section = title if level == 2 else ""
            nearest = title
            yield LineInfo(index, line, False, level, title, section, nearest)
            continue
        yield LineInfo(index, line, False, 0, "", section, nearest)


def section_bounds(lines: list[str], name: str, level: int = 2) -> tuple[int, int] | None:
    """Return (heading index, end index exclusive) for a heading with this exact text."""
    infos = list(walk("\n".join(lines)))
    start = None
    for info in infos:
        if info.in_code or not info.heading_level:
            continue
        if start is None:
            if info.heading_level == level and info.heading.casefold() == name.casefold():
                start = info.index
        elif info.heading_level <= level:
            return start, info.index
    if start is None:
        return None
    return start, len(lines)


def strip_code(text: str) -> str:
    """Blank out fenced code and inline code so links inside them are ignored."""
    out = []
    in_code = False
    for line in text.split("\n"):
        if FENCE_RE.match(line):
            in_code = not in_code
            out.append("")
            continue
        out.append("" if in_code else CODE_SPAN_RE.sub("", line))
    return "\n".join(out)


@dataclass(frozen=True)
class WikiLink:
    embed: bool
    target: str
    heading: str
    alias: str
    raw: str


def iter_links(text: str) -> Iterator[WikiLink]:
    for match in LINK_RE.finditer(strip_code(text)):
        inner = match.group(2).replace("\\|", "|")
        target_part, _, alias = inner.partition("|")
        target, _, heading = target_part.partition("#")
        yield WikiLink(bool(match.group(1)), target.strip(), heading.strip(), alias.strip(), match.group(0))
