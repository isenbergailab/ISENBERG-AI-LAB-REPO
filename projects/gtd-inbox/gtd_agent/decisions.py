"""'Decided:' captures: record a decision where the project keeps its decisions, and retire the questions it answers.

A project keeps decisions in its own note's `## Decisions` section, or in a separate `KEY_DECISIONS.md` note in
its folder (the hub note links it). Open questions live in the hub's `## Open questions` section and in the
separate note's `## Open decisions` section. Each answered question and each suggested step is its own checkbox.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .core import CONTEXTS, SECRET_RE
from .markdown import HEADING_RE, iter_links, section_bounds
from .tasks import TAG_RE, clean_text, normalize
from .vault import Project, Vault

DECIDED_RE = re.compile(r"(?is)^\s*decided\s*[:\-–]\s*(.+)$")
QUESTION_RE = re.compile(r"^(?:>\s*)?[-*+]\s+(?:\[( )\]\s+)?(.+?)\s*$")
BECAUSE_RE = re.compile(r"(?i)\s+because\s+|\s+·\s+")
MAX_QUESTIONS = 60
SHOWN_UNMATCHED = 6


def decisions_note(vault: Vault, project: Project) -> tuple[str, bool]:
    """(path, separate). A KEY_DECISIONS note in the project folder, or one the hub's Decisions section links."""
    folder = project.rel.rsplit("/", 1)[0]
    own = f"{folder}/{project.key}_DECISIONS.md"
    if vault.settings.vault_path(own).exists():
        return own, True
    note = vault.notes.get(project.rel)
    if note is not None:
        lines = note.text.split("\n")
        bounds = section_bounds(lines, "Decisions", 2)
        if bounds:
            for link in iter_links("\n".join(lines[bounds[0]:bounds[1]])):
                if link.target.upper().endswith("_DECISIONS"):
                    hits = vault.by_stem.get(link.target.casefold()) or []
                    if len(hits) == 1:
                        return hits[0], True
    return project.rel, False


def _section_questions(text: str, section: str, path: str, source: str) -> list[dict[str, Any]]:
    lines = text.split("\n")
    bounds = section_bounds(lines, section, 2)
    if bounds is None:
        return []
    found = []
    for line in lines[bounds[0] + 1:bounds[1]]:
        if line.startswith((" ", "\t")):
            continue
        match = QUESTION_RE.match(line.rstrip())
        if not match or not match.group(2).strip() or match.group(2).lstrip().startswith("[x]"):
            continue
        found.append({"text": match.group(2).strip(), "raw": line.rstrip(), "path": path, "source": source,
                      "tick": match.group(1) is not None})
    return found


def open_questions(vault: Vault, project: Project, decisions_rel: str, separate: bool) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    hub = vault.notes.get(project.rel)
    if hub is not None:
        questions += _section_questions(hub.text, "Open questions", project.rel, "Open questions")
    if separate and decisions_rel in vault.notes:
        questions += _section_questions(vault.notes[decisions_rel].text, "Open decisions", decisions_rel,
                                        "Open decisions")
    seen, unique = set(), []
    for question in questions:
        identity = (question["path"], question["raw"])
        if identity in seen:
            continue  # a line that appears twice cannot be retired safely by text
        seen.add(identity)
        unique.append(question)
    for number, question in enumerate(unique[:MAX_QUESTIONS], 1):
        question["id"] = f"q{number}"
    return unique[:MAX_QUESTIONS]


def decision_sections(text: str) -> list[str]:
    names = []
    for line in text.split("\n"):
        match = HEADING_RE.match(line)
        if match and len(match.group(1)) == 2:
            name = match.group(2).strip()
            if "open" not in name.casefold():
                names.append(name)
    return names


def _grounded(value: str, evidence: str, threshold: float = 0.6) -> bool:
    words = [w for w in normalize(value).split() if len(w) > 2]
    if not words:
        return False
    source = set(normalize(evidence).split())
    return sum(1 for w in words if w in source) / len(words) >= threshold


def plain_decision(text: str) -> tuple[str, str]:
    """Without a model: 'X because Y' or 'X · Y' becomes (X, Y). Links and tags are dropped."""
    body = re.sub(r"\[\[[^\]]+\]\]", "", text)
    body = TAG_RE.sub("", body)
    body = re.sub(r"\s+", " ", body).strip(" -·.")
    parts = BECAUSE_RE.split(body, maxsplit=1)
    decision = parts[0].strip(" ,.")
    why = parts[1].strip(" ,.") if len(parts) > 1 else ""
    return decision, why


def validate_answer(value: Any, capture: str, questions: list[dict[str, Any]], sections: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("the model answer is not an object")
    decision = re.sub(r"\s+", " ", str(value.get("decision") or "")).strip(" .")
    why = re.sub(r"\s+", " ", str(value.get("why") or "")).strip(" .")
    for field in (decision, why):
        if SECRET_RE.search(field) or "<!--" in field or "[[" in field or "*" in field:
            raise ValueError("unsafe decision text")
    if not decision or len(decision) > 200 or not _grounded(decision, capture):
        raise ValueError("the decision must reuse the capture's words")
    if why and (len(why) > 300 or not _grounded(why, capture)):
        why = ""
    ids = {q["id"] for q in questions}
    answered = [i for i in (value.get("question_ids") or []) if isinstance(i, str) and i in ids][:5]
    section = str(value.get("section") or "").strip()
    section = section if section in sections else ""
    steps = []
    for step in (value.get("steps") or [])[:3]:
        if not isinstance(step, dict):
            continue
        try:
            title = clean_text(str(step.get("title") or ""), 150)
        except ValueError:
            continue
        if "[[" in title or TAG_RE.search(title):
            continue
        steps.append({"title": title, "context": step.get("context") if step.get("context") in CONTEXTS
                      else "#computer"})
    return {"decision": decision, "why": why, "answered": answered, "section": section, "steps": steps,
            "explanation": re.sub(r"\s+", " ", str(value.get("explanation") or ""))[:300]}


def shown_questions(questions: list[dict[str, Any]], answered: list[str], capture: str) -> list[dict[str, Any]]:
    """Every question the decision answers, plus the few unmatched ones that share the most words with it."""
    words = {w for w in normalize(capture).split() if len(w) > 3}
    rest = [q for q in questions if q["id"] not in answered]
    rest.sort(key=lambda q: -len(words & set(normalize(q["text"]).split())))
    keep = {q["id"] for q in rest[:SHOWN_UNMATCHED]} | set(answered)
    return [q for q in questions if q["id"] in keep]


def decision_line(decision: str, why: str, day: date) -> str:
    return f"- **{decision}** · {why or 'rationale not stated'} · {day.isoformat()}"
