"""Task lines in the Obsidian Tasks plugin format: parse, format and edit safely."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .core import CONTEXTS, digest
from .markdown import walk

TASK_RE = re.compile(r"^(?P<indent>\s*)(?P<bullet>[-*+]|\d+[.)])\s+\[(?P<status>[^\]])\]\s?(?P<body>.*)$")
# Same tag pattern as the Tasks plugin, so agent and plugin agree on what a tag is.
TAG_RE = re.compile(r"(?:^|(?<=\s))#[^ \t!@#$%^&*(),.?\":{}|<>]+")
BLOCK_RE = re.compile(r"\s+\^([A-Za-z0-9-]+)\s*$")
DATE_FIELDS = {
    "📅": "due", "📆": "due", "🗓": "due", "🗓️": "due",
    "⏳": "scheduled", "⌛": "scheduled",
    "🛫": "start", "➕": "created", "✅": "done", "❌": "cancelled",
}
FIELD_EMOJI = {"due": "📅", "scheduled": "⏳", "start": "🛫", "created": "➕", "done": "✅", "cancelled": "❌"}
FIELD_ORDER = ("start", "scheduled", "due", "created", "done", "cancelled")
_date_alt = "|".join(sorted((re.escape(k) for k in DATE_FIELDS), key=len, reverse=True))
TRAILING = [
    ("date", re.compile(rf"\s*({_date_alt})️?\s*(\d{{4}}-\d{{2}}-\d{{2}})\s*$")),
    ("recurrence", re.compile(r"\s*🔁️?\s*([a-zA-Z0-9, !]+?)\s*$")),
    ("priority", re.compile(r"\s*(🔺|⏫|🔼|🔽|⏬)️?\s*$")),
    ("id", re.compile(r"\s*(🆔|⛔|🏁)️?\s*([A-Za-z0-9_,-]+)\s*$")),
    ("tag", re.compile(r"\s+(#[^ \t!@#$%^&*(),.?\":{}|<>]+)\s*$")),
]
ANY_DATE_RE = re.compile(rf"({_date_alt})️?\s*\d{{4}}-\d{{2}}-\d{{2}}")
TIME_TAG_RE = re.compile(r"^#(\d{1,3})(m|h)$")
CONTROL_TAG_RE = re.compile(r"(?i)(?:^|(?<=\s))#(next|waiting|someday|private|research)(?![^ \t!@#$%^&*(),.?\":{}|<>])")
WIKILINK_RE = re.compile(r"\[\[([^\]]+?)\]\]")


@dataclass
class Task:
    path: str
    line_no: int                 # 1-based
    raw: str
    indent: str
    status: str
    description: str             # text before trailing metadata, tags kept
    desc_end: int                # index in raw where trailing metadata begins
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    dates: dict[str, date] = field(default_factory=dict)
    recurrence: str | None = None
    block_id: str | None = None
    section: str = ""
    heading: str = ""
    metadata_ok: bool = True
    task_id: str | None = None                                # 🆔
    depends_on: list[str] = field(default_factory=list)       # ⛔ ids of tasks this one waits for

    @property
    def is_open(self) -> bool:
        return self.status in {" ", "/"}

    @property
    def is_done(self) -> bool:
        return self.status in {"x", "X"}

    @property
    def is_cancelled(self) -> bool:
        return self.status == "-"

    def has(self, tag: str) -> bool:
        return tag.casefold() in self.tags

    @property
    def is_waiting(self) -> bool:
        return self.has("#waiting")

    @property
    def is_next(self) -> bool:
        return self.has("#next")

    @property
    def context(self) -> str | None:
        return next((tag for tag in self.tags if tag in CONTEXTS), None)

    @property
    def minutes(self) -> int | None:
        for tag in self.tags:
            match = TIME_TAG_RE.match(tag)
            if match:
                value = int(match.group(1))
                return value * 60 if match.group(2) == "h" else value
        return None

    @property
    def due(self) -> date | None:
        return self.dates.get("due")

    @property
    def start(self) -> date | None:
        return self.dates.get("start")

    @property
    def created(self) -> date | None:
        return self.dates.get("created")

    @property
    def done(self) -> date | None:
        return self.dates.get("done")

    @property
    def title(self) -> str:
        """Readable text: tags removed, wikilinks shown as their display text."""
        text = TAG_RE.sub("", self.description)
        text = WIKILINK_RE.sub(lambda m: m.group(1).split("|")[-1].split("#")[0], text)
        return re.sub(r"\s+", " ", text).strip()

    @property
    def norm(self) -> str:
        return normalize(self.description)

    @property
    def key(self) -> str:
        return digest(f"{self.path}\n{self.norm}")[:16]

    @property
    def ref(self) -> str:
        """Stable reference for this exact line, used to match model output."""
        return digest(f"{self.path}\n{self.raw.strip()}")[:10]

    def waiting_parts(self) -> tuple[str, str]:
        text = TAG_RE.sub("", self.description).strip()
        text = WIKILINK_RE.sub("", text).strip()
        person, sep, what = text.partition(":")
        if not sep:
            return "", re.sub(r"\s+", " ", text).strip()
        return person.strip(), re.sub(r"\s+", " ", what).strip()


def normalize(text: str) -> str:
    text = TAG_RE.sub("", text)
    text = WIKILINK_RE.sub(lambda m: m.group(1).split("|")[0], text)
    text = re.sub(r"[^\w\s]", " ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def parse_task_line(line: str, path: str = "", line_no: int = 0) -> Task | None:
    match = TASK_RE.match(line)
    if not match:
        return None
    body = match.group("body")
    prefix_len = len(line) - len(body)
    block_id = None
    block = BLOCK_RE.search(body)
    if block:
        block_id = block.group(1)
        body = body[:block.start()]
    dates: dict[str, date] = {}
    recurrence = None
    task_id, depends_on = None, []
    rest = body.rstrip()
    changed = True
    while changed and rest:
        changed = False
        for kind, pattern in TRAILING:
            found = pattern.search(rest)
            if not found:
                continue
            if kind == "date":
                name = DATE_FIELDS.get(found.group(1)) or DATE_FIELDS.get(found.group(1).rstrip("️"))
                try:
                    value = date.fromisoformat(found.group(2))
                except ValueError:
                    value = None
                if name and value and name not in dates:
                    dates[name] = value
            elif kind == "recurrence":
                recurrence = found.group(1).strip()
            elif kind == "id":
                if found.group(1).startswith("🆔"):
                    task_id = found.group(2)
                elif found.group(1).startswith("⛔"):
                    depends_on = [part for part in found.group(2).split(",") if part]
            rest = rest[:found.start()].rstrip()
            changed = True
            break
    # Trailing tags were stripped while scanning; description keeps every tag.
    head_len = len(rest)
    description_end = head_len
    tail = body[head_len:]
    tail_tags = TAG_RE.findall(tail)
    description = (rest + (" " + " ".join(tail_tags) if tail_tags else "")).strip()
    tags = [tag.casefold() for tag in TAG_RE.findall(rest)] + [tag.casefold() for tag in tail_tags]
    links = [m.group(1).split("|")[0].split("#")[0].strip() for m in WIKILINK_RE.finditer(description)]
    metadata_ok = not ANY_DATE_RE.search(rest) and "🔁" not in rest
    return Task(path=path, line_no=line_no, raw=line, indent=match.group("indent"), status=match.group("status"),
                description=description, desc_end=prefix_len + description_end, tags=tags, links=links,
                dates=dates, recurrence=recurrence, block_id=block_id, metadata_ok=metadata_ok, task_id=task_id,
                depends_on=depends_on)


def parse_tasks(text: str, path: str = "") -> list[Task]:
    found = []
    for info in walk(text):
        if info.in_code or info.heading_level:
            continue
        task = parse_task_line(info.text, path, info.index + 1)
        if task is not None:
            task.section = info.section
            task.heading = info.nearest
            found.append(task)
    return found


# ---------------------------------------------------------------- formatting
def plain_words(text: str) -> str:
    """Words the agent writes into a task stay words: a model's '#next' in a title becomes 'next', not a tag."""
    return CONTROL_TAG_RE.sub(lambda match: match.group(1), text)


def clean_text(value: str, maximum: int = 500) -> str:
    value = re.sub(r"\s+", " ", value.replace("\r", " ").replace("\n", " ")).strip()
    if not value or len(value) > maximum or "<!--" in value or "-->" in value:
        raise ValueError("Task text must be one line of 1-500 characters without HTML comments")
    if ANY_DATE_RE.search(value) or "🔁" in value or re.search(r"\^[A-Za-z0-9-]+\s*$", value):
        raise ValueError("Task text cannot contain Tasks dates, repeats or block ids")
    return value


def format_task(text: str, tags: list[str] | None = None, *, dates: dict[str, date | None] | None = None,
                recurrence: str | None = None, block_id: str | None = None, status: str = " ") -> str:
    parts = [f"- [{status}] {clean_text(text)}"]
    existing = {tag.casefold() for tag in TAG_RE.findall(text)}
    for tag in tags or []:
        if tag and tag.casefold() not in existing:
            parts.append(tag)
            existing.add(tag.casefold())
    if recurrence:
        parts.append(f"🔁 {recurrence}")
    for name in FIELD_ORDER:
        value = (dates or {}).get(name)
        if value:
            parts.append(f"{FIELD_EMOJI[name]} {value.isoformat()}")
    line = " ".join(parts)
    if block_id:
        line += f" ^{block_id}"
    return line


def text_before_dates(line: str) -> str | None:
    """'Call Bob 📅 2026-10-01 about rent #calls' becomes 'Call Bob about rent #calls 📅 2026-10-01', so the Tasks
    plugin reads the date. None when a person must decide: a repeat rule mid-text, or a date field given twice."""
    task = parse_task_line(line)
    match = TASK_RE.match(line)
    if task is None or match is None or task.metadata_ok:
        return None
    head, block = _split_block(line)
    start = match.start("body")
    rest, tail = head[start:task.desc_end], head[task.desc_end:]
    if "🔁" in rest:
        return None
    moved, fields = [], []
    for found in ANY_DATE_RE.finditer(rest):
        name = DATE_FIELDS.get(found.group(1)) or DATE_FIELDS.get(found.group(1).rstrip("️"))
        moved.append(f"{FIELD_EMOJI[name]} {found.group(0)[-10:]}")
        fields.append(name)
    if len(set(fields)) != len(fields) or any(name in task.dates for name in fields):
        return None
    text = re.sub(r"\s+", " ", ANY_DATE_RE.sub(" ", rest)).strip()
    tags = re.match(r"(?:\s+#[^ \t!@#$%^&*(),.?\":{}|<>]+)*", tail).group(0)
    fixed = f"{head[:start]}{text}{tags} {' '.join(moved)}{tail[len(tags):]}{block}"
    check = parse_task_line(fixed)
    if check is None or not check.metadata_ok or any(check.dates.get(name) is None for name in fields):
        return None
    return fixed


def _split_block(line: str) -> tuple[str, str]:
    block = BLOCK_RE.search(line)
    if block:
        return line[:block.start()], line[block.start():]
    return line.rstrip(), ""


def with_status(line: str, status: str, done_on: date | None = None) -> str:
    task = parse_task_line(line)
    if task is None:
        raise ValueError("Not a task line")
    match = TASK_RE.match(line)
    assert match is not None
    start = match.start("status")
    updated = line[:start] + status + line[start + 1:]
    if status in {"x", "X"} and done_on and "done" not in task.dates:
        head, block = _split_block(updated)
        updated = f"{head.rstrip()} ✅ {done_on.isoformat()}{block}"
    if status == "-" and done_on and "cancelled" not in task.dates:
        head, block = _split_block(updated)
        updated = f"{head.rstrip()} ❌ {done_on.isoformat()}{block}"
    return updated


def with_tag(line: str, tag: str) -> str:
    task = parse_task_line(line)
    if task is None:
        raise ValueError("Not a task line")
    if task.has(tag):
        return line
    cut = task.desc_end
    return line[:cut].rstrip() + f" {tag}" + (" " + line[cut:].lstrip() if line[cut:].strip() else "")


def without_tag(line: str, tag: str) -> str:
    """Remove one tag from a task line, leaving everything else as it was."""
    if parse_task_line(line) is None:
        raise ValueError("Not a task line")
    pattern = re.compile(r"[ \t]+" + re.escape(tag) + r"(?![^ \t!@#$%^&*(),.?\":{}|<>])", re.IGNORECASE)
    return pattern.sub("", line)


def without_dependency(line: str, task_id: str) -> str:
    """Remove one id from a task's ⛔ list; drop the ⛔ entirely when it was the only one."""
    match = re.search(r"(\s*)⛔️?\s*([A-Za-z0-9_,-]+)", line)
    if match is None:
        return line
    kept = [part for part in match.group(2).split(",") if part and part != task_id]
    replacement = f"{match.group(1)}⛔ {','.join(kept)}" if kept else ""
    return line[:match.start()] + replacement + line[match.end():]


def with_dependency(line: str, task_id: str) -> str:
    """Add one id to a task's ⛔ list, creating the list before any block id."""
    match = re.search(r"⛔️?\s*([A-Za-z0-9_,-]+)", line)
    if match:
        ids = [part for part in match.group(1).split(",") if part]
        if task_id in ids:
            return line
        return line[:match.start(1)] + ",".join([*ids, task_id]) + line[match.end(1):]
    head, block = _split_block(line)
    return f"{head.rstrip()} ⛔ {task_id}{block}"


def with_id(line: str, task_id: str) -> str:
    """Give a task line its 🆔, before any block id."""
    head, block = _split_block(line)
    return f"{head.rstrip()} 🆔 {task_id}{block}"


def with_date(line: str, name: str, value: date) -> str:
    if name not in FIELD_EMOJI:
        raise ValueError("Unknown date field")
    task = parse_task_line(line)
    if task is None:
        raise ValueError("Not a task line")
    head, block = _split_block(line)
    emojis = [key for key, field_name in DATE_FIELDS.items() if field_name == name]
    pattern = re.compile(r"(" + "|".join(re.escape(e) for e in sorted(emojis, key=len, reverse=True))
                         + r")️?\s*\d{4}-\d{2}-\d{2}")
    region_start = task.desc_end
    region = head[region_start:]
    if pattern.search(region):
        region = pattern.sub(f"{FIELD_EMOJI[name]} {value.isoformat()}", region, count=1)
        return head[:region_start] + region + block
    return f"{head.rstrip()} {FIELD_EMOJI[name]} {value.isoformat()}{block}"


def actionable_rule(task: Task, file_status: str | None, today: date, single_actions: str) -> bool:
    """Mirror the Next Actions query: open, not waiting, started, #next or a single action."""
    if not task.is_open or task.is_waiting:
        return False
    if task.start and task.start > today:
        return False
    if file_status in {"someday", "done", "dropped"}:
        return False
    return task.is_next or task.path == single_actions
