#!/usr/bin/env python3
"""Render Anki card templates for configured field names."""

from __future__ import annotations

import argparse
from pathlib import Path

from automatic_anki_core import ROLES, load_runtime

DEFAULT_FIELDS = {
    "Prompt": "Front",
    "Answer": "Back",
    "Notes": "Notes",
    "Example": "Example",
}


def render_templates(config_path: Path, output_directory: Path) -> list[Path]:
    config, _profile = load_runtime(config_path)
    mapping = config["field_mapping"]
    for role in ROLES:
        if any(token in mapping[role] for token in ("{{", "}}", "\n", "\r", "\t")):
            raise ValueError(f"unsafe Anki field name for {role}")

    template_root = Path(__file__).resolve().parents[1] / "templates" / "anki"
    output_directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for filename in ("front.html", "back.html"):
        content = (template_root / filename).read_text(encoding="utf-8")
        for role, default_field in DEFAULT_FIELDS.items():
            content = content.replace(
                "{{" + default_field + "}}",
                "{{" + mapping[role] + "}}",
            )
            content = content.replace(
                "{{#" + default_field + "}}",
                "{{#" + mapping[role] + "}}",
            )
            content = content.replace(
                "{{/" + default_field + "}}",
                "{{/" + mapping[role] + "}}",
            )
        destination = output_directory / filename
        destination.write_text(content, encoding="utf-8")
        written.append(destination)

    styling = output_directory / "styling.css"
    styling.write_text((template_root / "styling.css").read_text(encoding="utf-8"), encoding="utf-8")
    written.append(styling)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = render_templates(args.config.resolve(), args.output_directory.resolve())
    except (OSError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
