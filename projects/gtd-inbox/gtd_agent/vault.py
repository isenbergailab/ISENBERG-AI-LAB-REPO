"""Read-only model of vault v2: projects, areas, tasks, links and privacy."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .core import Settings, read_snapshot
from .markdown import iter_links, link_target, parse_frontmatter
from .tasks import Task, actionable_rule, parse_tasks

KEY_RE = re.compile(r"[A-Z0-9]+(?:_[A-Z0-9]+)*")
STATUSES = ("active", "waiting", "someday", "done", "dropped")
PRIORITIES = ("high", "medium", "low")
TASK_EXCLUDE = ("04_REFERENCE", "05_ARCHIVE", "_agent", "00_INBOX", ".obsidian", ".trash")


def project_key(name: str) -> str:
    key = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    if not key or len(key) > 80 or not KEY_RE.fullmatch(key):
        raise ValueError("Project name cannot form a valid ALL_CAPS key")
    return key


def _date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


@dataclass
class Note:
    rel: str
    stem: str
    frontmatter: dict[str, Any]
    text: str
    mtime: float


@dataclass
class Project:
    key: str
    rel: str
    folder: str
    frontmatter: dict[str, Any]
    status: str
    area: str | None
    due: date | None
    priority: str
    aliases: list[str]
    review: date | None
    private_flag: bool
    archived: bool
    mtime: float
    tasks: list[Task] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class Area:
    key: str
    rel: str
    aliases: list[str]
    private: bool
    tasks: list[Task] = field(default_factory=list)


class Vault:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.root
        self.notes: dict[str, Note] = {}
        self.by_stem: dict[str, list[str]] = {}
        self.files: set[str] = set()
        self.file_names: dict[str, list[str]] = {}
        self.projects: dict[str, Project] = {}
        self.areas: dict[str, Area] = {}
        self.tasks: list[Task] = []
        self.file_status: dict[str, str] = {}
        self.stray_projects: list[str] = []
        self.load()

    # ------------------------------------------------------------ loading
    def load(self) -> None:
        for path in sorted(self.root.rglob("*")):
            rel = path.relative_to(self.root).as_posix()
            if rel.startswith((".obsidian", ".trash")) or "/.obsidian" in rel or not path.is_file():
                continue
            self.files.add(rel)
            self.file_names.setdefault(path.name.casefold(), []).append(rel)
            if path.suffix.lower() != ".md":
                continue
            try:
                text = read_snapshot(path).text
            except (OSError, UnicodeError):
                continue
            note = Note(rel, path.stem, parse_frontmatter(text), text, path.stat().st_mtime)
            self.notes[rel] = note
            self.by_stem.setdefault(path.stem.casefold(), []).append(rel)
        self._load_areas()
        self._load_projects()
        self._load_tasks()

    def _load_areas(self) -> None:
        base = self.settings.areas_dir + "/"
        for rel, note in self.notes.items():
            if not rel.startswith(base) or note.frontmatter.get("type") != "area":
                continue
            key = str(note.frontmatter.get("key") or note.stem)
            self.areas[key] = Area(key, rel, _list(note.frontmatter.get("aliases")),
                                   bool(note.frontmatter.get("private", False)))

    def _load_projects(self) -> None:
        for base, archived in ((self.settings.projects_dir, False), (self.settings.archive_dir, True)):
            prefix = base + "/"
            for rel, note in self.notes.items():
                if not rel.startswith(prefix):
                    continue
                parts = rel[len(prefix):].split("/")
                is_hub = len(parts) == 2 and parts[1] == parts[0] + ".md"
                if note.frontmatter.get("type") != "project":
                    if not archived and len(parts) == 2 and parts[1] == parts[0] + ".md":
                        self.stray_projects.append(rel)
                    continue
                if not is_hub:
                    continue
                fm = note.frontmatter
                key = str(fm.get("key") or note.stem)
                problems = []
                if key != note.stem:
                    problems.append(f"key `{key}` differs from the file name")
                if not KEY_RE.fullmatch(key):
                    problems.append("key is not ALL_CAPS_WITH_UNDERSCORES")
                status = str(fm.get("status") or "").casefold()
                if status not in STATUSES:
                    problems.append(f"status `{fm.get('status')}` is not one of {', '.join(STATUSES)}")
                priority = str(fm.get("priority") or "").casefold()
                if priority and priority not in PRIORITIES:
                    problems.append(f"priority `{fm.get('priority')}` is not high, medium or low")
                if fm.get("due") not in (None, "") and _date(fm.get("due")) is None:
                    problems.append("due is not a YYYY-MM-DD date")
                area = link_target(fm.get("area"))
                folder = str(Path(rel).parent.as_posix())
                mtime = max((n.mtime for r, n in self.notes.items() if r.startswith(folder + "/")), default=note.mtime)
                self.projects[key] = Project(
                    key=key, rel=rel, folder=folder, frontmatter=fm, status=status or "active", area=area,
                    due=_date(fm.get("due")), priority=priority, aliases=_list(fm.get("aliases")),
                    review=_date(fm.get("review")), private_flag=bool(fm.get("private", False)),
                    archived=archived, mtime=mtime, problems=problems)
                self.file_status[rel] = status or "active"
        for project in self.projects.values():
            if project.area and project.area not in self.areas:
                project.problems.append(f"area `{project.area}` is not a note in {self.settings.areas_dir}")

    def _load_tasks(self) -> None:
        for rel, note in self.notes.items():
            if rel.startswith(TASK_EXCLUDE) or rel.startswith(self.settings.reviews_dir + "/"):
                continue
            tasks = parse_tasks(note.text, rel)
            if not tasks:
                continue
            self.tasks.extend(tasks)
            project = self.project_for_path(rel)
            if project is not None and rel == project.rel:
                project.tasks = tasks
            for area in self.areas.values():
                if area.rel == rel:
                    area.tasks = tasks

    # ------------------------------------------------------------ lookups
    def project_for_path(self, rel: str) -> Project | None:
        for project in self.projects.values():
            if rel == project.rel or rel.startswith(project.folder + "/"):
                return project
        return None

    def area_for_path(self, rel: str) -> Area | None:
        return next((area for area in self.areas.values() if area.rel == rel), None)

    def active_projects(self) -> list[Project]:
        return [p for p in self.projects.values() if not p.archived and p.status == "active"]

    def is_private_project(self, project: Project) -> bool:
        if project.private_flag:
            return True
        area = self.areas.get(project.area or "")
        return bool(area and area.private)

    def is_private_task(self, task: Task) -> bool:
        if task.has("#private"):
            return True
        project = self.project_for_path(task.path)
        if project is not None and self.is_private_project(project):
            return True
        area = self.area_for_path(task.path)
        if area is not None and area.private:
            return True
        for link in task.links:
            linked = self.projects.get(link)
            if linked is not None and self.is_private_project(linked):
                return True
            linked_area = self.areas.get(link)
            if linked_area is not None and linked_area.private:
                return True
        return False

    def status_for(self, rel: str) -> str | None:
        project = self.project_for_path(rel)
        if project is not None:
            return project.status
        return None

    def actionable(self, today: date) -> list[Task]:
        return [task for task in self.tasks
                if actionable_rule(task, self.status_for(task.path), today, self.settings.single_actions)]

    def waiting(self) -> list[Task]:
        return [task for task in self.tasks if task.is_open and task.is_waiting
                and self.status_for(task.path) not in {"done", "dropped"}]

    def open_next(self, project: Project) -> list[Task]:
        return [t for t in project.tasks if t.is_open and t.is_next and not t.is_waiting]

    def first_open_step(self, project: Project) -> Task | None:
        for task in project.tasks:
            if task.section.casefold() != "steps" or task.indent or not task.is_open or task.is_waiting:
                continue
            if task.heading.casefold() == "done":
                continue
            return task
        return None

    def contains_path(self, rel: str) -> bool:
        return rel in self.files

    # ------------------------------------------------------------ links
    def resolve(self, target: str) -> str | None:
        target = target.strip()
        if not target:
            return None
        if "/" in target:
            candidates = [target, target + ".md"]
            for candidate in candidates:
                if candidate in self.files:
                    return candidate
            name = target.rsplit("/", 1)[-1]
        else:
            name = target
        lowered = name.casefold()
        if lowered.endswith(".md"):
            lowered = lowered[:-3]
        hits = self.by_stem.get(lowered)
        if hits:
            return hits[0]
        hits = self.file_names.get(name.casefold())
        if hits:
            return hits[0]
        return None

    def alias_index(self) -> dict[str, set[str]]:
        index: dict[str, set[str]] = {}
        for rel, note in self.notes.items():
            if rel.startswith((self.settings.archive_dir + "/",)):
                continue
            for alias in _list(note.frontmatter.get("aliases")):
                index.setdefault(alias.casefold(), set()).add(note.stem)
            index.setdefault(note.stem.casefold(), set()).add(note.stem)
        return index

    def broken_links(self, skip_prefixes: tuple[str, ...]) -> list[tuple[str, str]]:
        found = []
        for rel, note in self.notes.items():
            if rel.startswith(skip_prefixes):
                continue
            for link in iter_links(note.text):
                if link.target and self.resolve(link.target) is None:
                    found.append((rel, link.target))
        return found

    def key_names(self) -> set[str]:
        return {stem for stem in (note.stem for note in self.notes.values())}

    def mtime_date(self, project: Project) -> date:
        return datetime.fromtimestamp(project.mtime).date()
