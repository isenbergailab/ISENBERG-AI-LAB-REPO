"""Reviewed, one-way links from the TOPIC project to an index of public TOPIC notes."""
from __future__ import annotations

import json
from typing import Any

from .core import digest
from .documents import DocumentIntake
from .markdown import iter_links, link_target
from .ops import LINKS_BEGIN, LINKS_END
from .providers import ProviderError
from .vault import Project, Vault
from .workflow import PROPOSAL_VERSION, Agent


class ReferenceLinker:
    def __init__(self, agent: Agent):
        self.agent = agent

    def _eligible(self, vault: Vault) -> tuple[str, list[Any]] | None:
        prefix = self.agent.settings.reference_dir + "/TOPIC/"
        index = prefix + "TOPIC_INDEX.md"
        if index not in vault.notes:
            return None
        notes = [note for rel, note in sorted(vault.notes.items()) if rel.startswith(prefix)]
        def flagged(note: Any) -> bool:
            private = note.frontmatter.get("private")
            marked = private not in (None, False, "", "false", "False")
            folder = any(part.upper() == "PRIVATE" for part in note.rel.split("/"))
            return marked or folder or bool(DocumentIntake.private_reason(vault, note.text, note.frontmatter))
        blocked = {note.rel for note in vault.notes.values() if flagged(note)}
        changed = True
        while changed:
            changed = False
            for note in notes:
                if note.rel in blocked:
                    continue
                if any(vault.resolve(link.target) in blocked for link in iter_links(note.text)):
                    blocked.add(note.rel)
                    changed = True
        public = [note for note in notes if note.rel not in blocked]
        if index not in {note.rel for note in public}:
            return None
        return index, [note for note in public if note.rel != index]

    def _project(self, vault: Vault, index: str, notes: list[Any]) -> Project | None:
        saved = self.agent.ledger.kv_get("reference_project|TOPIC")
        if saved:
            project = vault.projects.get(saved)
            if project and not project.archived and not vault.is_private_project(project):
                return project
        explicit = link_target(vault.notes[index].frontmatter.get("project"))
        project = vault.projects.get(explicit or "")
        if project and not project.archived and not vault.is_private_project(project):
            return project
        candidates = [p for p in vault.projects.values() if not p.archived and not vault.is_private_project(p)]
        named = [p for p in candidates if "TOPIC" in p.key.split("_") or any(
            "TOPIC" in alias.upper().split() for alias in p.aliases)]
        if len(named) == 1:
            return named[0]
        if not self.agent.settings.remote_inference or not self.agent._provider_ready():
            return None
        metadata = [{"path": note.rel, "title": str(note.frontmatter.get("title") or note.stem),
                     "aliases": note.frontmatter.get("aliases", []), "tags": note.frontmatter.get("tags", [])}
                    for note in notes[:30]]
        choices = [{"key": p.key, "aliases": p.aliases} for p in candidates]
        if not choices:
            return None
        cache_key = "reference_match|TOPIC|" + digest(json.dumps([metadata, choices], sort_keys=True, default=str))[:20]
        cached = self.agent.ledger.kv_get(cache_key)
        if cached is not None:
            return vault.projects.get(cached) if cached else None
        try:
            result = self.agent.provider.reference_match("TOPIC", metadata, choices)
            if result["need_excerpt"] and not result["project_key"]:
                excerpts = [{"path": note.rel, "excerpt": "\n".join(note.text.splitlines()[:50])[:6000]}
                            for note in notes[:10]]
                result = self.agent.provider.reference_match("TOPIC", metadata, choices, excerpts)
        except ProviderError:
            return None
        self.agent.ledger.kv_set(cache_key, result["project_key"])
        return vault.projects.get(result["project_key"]) if result["project_key"] else None

    @staticmethod
    def _marked(text: str) -> list[str] | None:
        lines = text.splitlines()
        if LINKS_BEGIN not in lines and LINKS_END not in lines:
            return []
        if lines.count(LINKS_BEGIN) != 1 or lines.count(LINKS_END) != 1:
            return None
        start, end = lines.index(LINKS_BEGIN), lines.index(LINKS_END)
        return lines[start + 1:end] if start < end else None

    @staticmethod
    def _desired(vault: Vault, index: str, notes: list[Any]) -> list[str]:
        text = vault.notes[index].text
        lines = text.splitlines()
        if LINKS_BEGIN in lines and LINKS_END in lines:
            text = "\n".join(lines[:lines.index(LINKS_BEGIN)] + lines[lines.index(LINKS_END) + 1:])
        manual = {vault.resolve(link.target) for link in iter_links(text)}
        return [f"- [[{note.rel.removesuffix('.md')}]]" for note in notes if note.rel not in manual]

    def run(self, vault: Vault) -> list[str]:
        if self.agent.settings.mode != "approval":
            return []
        eligible = self._eligible(vault)
        if eligible is None:
            return []
        index, notes = eligible
        project = self._project(vault, index, notes)
        if project is None:
            signature = digest(json.dumps([note.rel for note in notes]))[:20]
            key = f"reference_match|TOPIC|{signature}"
            if self.agent.ledger.get_by_key(key) is None:
                self.agent.ledger.upsert("reference", key, {
                    "engine_version": PROPOSAL_VERSION, "kind": "reference_match", "title": "Connect TOPIC notes",
                    "source_paths": [note.rel for note in notes], "ops": [],
                    "reason": "Which existing public project should the TOPIC index link to? Enter its key below.",
                })
                return ["TOPIC project needs your answer"]
            return []
        old = self._marked(vault.notes[index].text)
        if old is None:
            return []  # preserve a broken or hand-edited marked section
        desired = self._desired(vault, index, notes)
        ops: list[dict[str, Any]] = []
        if old != desired:
            ops.append({"op": "set_marked_links", "path": index, "old": old, "lines": desired})
        index_link = "[[TOPIC_INDEX]]"
        if index_link not in vault.notes[project.rel].text:
            ops.append({"op": "append_bullet", "path": project.rel, "section": "Links", "text": index_link})
        if not ops:
            return []
        signature = digest(json.dumps({"project": project.key, "old": old, "new": desired,
                                       "project_link": len(ops) == 2}, sort_keys=True))[:20]
        key = f"reference_links|TOPIC|{signature}"
        if self.agent.ledger.get_by_key(key) is not None:
            return []
        for row in self.agent.ledger.list("pending", "reference"):
            if row["dedupe_key"].startswith("reference_links|TOPIC|"):
                self.agent.ledger.set_status(row["id"], "stale")
        proposal = {"engine_version": PROPOSAL_VERSION, "kind": "reference_links", "title": "Connect TOPIC notes",
                    "project": project.key, "summary": f"[[{project.key}]] → [[TOPIC_INDEX]] → {len(notes)} TOPIC notes",
                    "reason": "The TOPIC folder and project appear related. Review every link before saving.",
                    "source_paths": [note.rel for note in notes], "ops": ops}
        self.agent.ledger.upsert("reference", key, proposal)
        return [f"TOPIC links proposed: {len(notes)} public notes"]
