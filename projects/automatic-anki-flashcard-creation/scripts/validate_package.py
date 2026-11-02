#!/usr/bin/env python3
"""Run structural and runtime checks for the published skill package."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from automatic_anki_core import load_runtime, validate_profile  # noqa: E402
from validate_export import validate_export  # noqa: E402

REQUIRED = (
    "SKILL.md",
    "README.md",
    "DESIGN.md",
    "GLOSSARY.md",
    "LICENSE",
    ".gitignore",
    "config/schema.json",
    "config/example.json",
    "language-profiles/schema.json",
    "language-profiles/japanese.json",
    "language-profiles/spanish.json",
    "language-profiles/mandarin.json",
    "language-profiles/template.json",
    "fixtures/sources/text-cat.json",
    "fixtures/sources/image-eat.json",
    "fixtures/sources/ambiguous-image.json",
    "fixtures/sources/duplicate-cat.json",
    "fixtures/sources/html-injection.json",
    "fixtures/sources/malformed.json",
    "fixtures/sources/spanish-casa.json",
    "fixtures/sources/mandarin-cat.json",
    "templates/anki/front.html",
    "templates/anki/back.html",
    "templates/anki/styling.css",
    "references/anki-beginner-guide.md",
    "references/hermes-beginner-guide.md",
    "references/telegram-setup.md",
    "references/discord-setup.md",
    "references/message-adapter-contract.md",
    "references/language-profile-spec.md",
    "references/validation-and-export.md",
    "scripts/automatic_anki_core.py",
    "scripts/render_anki_templates.py",
    "scripts/validate_export.py",
    "scripts/validate_package.py",
    "tests/test_core.py",
    "tests/test_render_anki_templates.py",
    "tests/test_validate_export.py",
    "tests/test_package.py",
    "docs/adr/0001-separate-core-from-messaging-services.md",
    "docs/adr/0002-reject-ambiguous-cards.md",
    "examples/Alpha.tsv",
    "examples/Alpha.validation.json",
)


def validate_skill(root: Path) -> list[str]:
    errors: list[str] = []
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    if not skill.startswith("---\n"):
        errors.append("SKILL.md must begin with YAML frontmatter")
    frontmatter_match = re.match(r"---\n(.*?)\n---\n", skill, re.DOTALL)
    if not frontmatter_match:
        errors.append("SKILL.md frontmatter is not closed")
        return errors

    frontmatter = frontmatter_match.group(1)
    required_fields = ("name", "description", "version", "author", "license", "platforms")
    for field in required_fields:
        if not re.search(rf"^{field}:\s*.+$", frontmatter, re.MULTILINE):
            errors.append(f"SKILL.md frontmatter is missing {field}")

    name = re.search(r"^name:\s*(.+)$", frontmatter, re.MULTILINE)
    description = re.search(r"^description:\s*(.+)$", frontmatter, re.MULTILINE)
    if not name or name.group(1).strip() != "automatic-anki-flashcard-creation":
        errors.append("SKILL.md name is incorrect")
    if description:
        value = description.group(1).strip().strip('"')
        if len(value) > 60:
            errors.append("SKILL.md description exceeds 60 characters")
        if not value.endswith("."):
            errors.append("SKILL.md description must end with a period")
    return errors


def validate_package(root: Path) -> list[str]:
    errors: list[str] = []
    for relative in REQUIRED:
        if not (root / relative).is_file():
            errors.append(f"missing required file: {relative}")
    if errors:
        return errors

    errors.extend(validate_skill(root))

    for path in sorted(root.rglob("*.json")):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"invalid JSON {path.relative_to(root)}: {exc}")

    try:
        load_runtime(root / "config" / "example.json")
    except ValueError as exc:
        errors.append(f"example config failed runtime validation: {exc}")

    for name in ("japanese", "spanish", "mandarin"):
        try:
            profile = json.loads(
                (root / "language-profiles" / f"{name}.json").read_text(encoding="utf-8")
            )
            validate_profile(profile)
        except (ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{name} Language Profile failed validation: {exc}")

    try:
        validate_export(
            root / "examples" / "Alpha.tsv",
            root / "examples" / "Alpha.validation.json",
            expected_count=2,
        )
    except (OSError, ValueError) as exc:
        errors.append(f"example export failed validation: {exc}")

    forbidden_path_markers = ("/" + "Users/", "C:" + "\\Users\\")
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {"DESIGN.md", "validate_package.py"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(marker in text for marker in forbidden_path_markers):
            errors.append(f"machine-local path in {path.relative_to(root)}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()

    errors = validate_package(args.root.resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"package checks passed: {args.root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
