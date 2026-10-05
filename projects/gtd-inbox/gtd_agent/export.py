"""Project export: one Markdown file with everything about a project, for reading cold or handing to someone.

The file holds the project note, its decisions note, every note either of them links, and the reference notes
whose `project` property names it, each once. It goes to state/exports/, outside the vault and its synced folder. A private
project, or one that pulls in a private note, gets a banner on top.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .core import Settings
from .decisions import decisions_note
from .markdown import iter_links, split_frontmatter
from .vault import Project, Vault

BANNER = ("> [!warning] Private. This export holds private notes: keep it out of shared folders, sync and any "
          "model.")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^(#{1,6})(\s)")


def find_project(vault: Vault, key: str) -> Project:
    project = vault.projects.get(key) or next(
        (p for p in vault.projects.values() if p.key.casefold() == key.casefold()), None)
    if project is None:
        raise ValueError(f"No project named {key}. Use the key from the note's properties, such as DEMO_PROJECT")
    return project


def _names(value: object, key: str) -> bool:
    values = value if isinstance(value, list) else [value]
    for item in values:
        text = str(item or "")
        targets = [link.target for link in iter_links(text)] or [text.strip()]
        if any(target.casefold() == key.casefold() for target in targets):
            return True
    return False


def notes_for(vault: Vault, project: Project) -> list[str]:
    """The notes an export holds, in reading order, each once."""
    order = [project.rel]
    target, separate = decisions_note(vault, project)
    if separate and target in vault.notes:
        order.append(target)
    for source in list(order):
        for link in iter_links(vault.notes[source].text):
            rel = vault.resolve(link.target)
            if rel and rel in vault.notes and rel not in order \
                    and not rel.startswith(vault.settings.agent_dir + "/"):
                order.append(rel)
    reference = vault.settings.reference_dir + "/"
    for rel in sorted(vault.notes):
        if rel.startswith(reference) and rel not in order and _names(vault.notes[rel].frontmatter.get("project"),
                                                                      project.key):
            order.append(rel)
    return order


def _body(vault: Vault, rel: str) -> list[str]:
    """A note's text for the bundle: properties as a yaml block, its title dropped, headings two levels down."""
    note = vault.notes[rel]
    front, start = split_frontmatter(note.text)
    lines = "\n".join(note.text.split("\n")[start:]).strip("\n").split("\n")
    if lines and lines[0].strip().casefold() == f"# {note.stem}".casefold():
        lines = "\n".join(lines[1:]).lstrip("\n").split("\n")
    out = ["```yaml", *front.strip("\n").split("\n"), "```", ""] if front and front.strip() else []
    fenced = False
    for line in lines:
        if FENCE_RE.match(line):
            fenced = not fenced
        elif not fenced and (heading := HEADING_RE.match(line)):
            line = "#" * min(6, len(heading.group(1)) + 2) + line[len(heading.group(1)):]
        out.append(line)
    return out


def _private(vault: Vault, project: Project, rels: list[str]) -> bool:
    if vault.is_private_project(project):
        return True
    return any(str(vault.notes[rel].frontmatter.get("private", "")).casefold() == "true"
               or "#private" in vault.notes[rel].text.casefold() for rel in rels)


def export_project(settings: Settings, key: str, now: datetime) -> Path:
    vault = Vault(settings)
    project = find_project(vault, key)
    rels = notes_for(vault, project)
    lines = [f"# Export: {project.key}", ""]
    if _private(vault, project, rels):
        lines += [BANNER, ""]
    lines += [f"Exported {now:%Y-%m-%d %H:%M} by the GTD agent. {len(rels)} notes: the project, its decisions note, "
              "the notes they link and the reference notes that name it. Each appears once.", "", "## Contents",
              *[f"{number}. {vault.notes[rel].stem} · {rel}" for number, rel in enumerate(rels, 1)], ""]
    for rel in rels:
        lines += [f"## {vault.notes[rel].stem}", f"`{rel}`", "", *_body(vault, rel), ""]
    path = settings.state_dir / "exports" / f"{project.key}-{now:%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    return path
