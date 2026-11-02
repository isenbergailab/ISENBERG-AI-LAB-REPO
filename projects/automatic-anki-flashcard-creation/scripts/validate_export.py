#!/usr/bin/env python3
"""Validate a four-field Anki TSV and its companion report."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROLES = ("Prompt", "Answer", "Notes", "Example")
PROVENANCE = {"source", "reference", "generated"}
SOURCE_TYPES = {"text", "image", "list-item"}
DUPLICATE_POLICIES = {"reject", "merge", "allow"}
NATO_WORDS = (
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf",
    "Hotel", "India", "Juliett", "Kilo", "Lima", "Mike", "November",
    "Oscar", "Papa", "Quebec", "Romeo", "Sierra", "Tango", "Uniform",
    "Victor", "Whiskey", "Xray", "Yankee", "Zulu",
)
NATO_PATTERN = re.compile(
    rf"^(?:{'|'.join(NATO_WORDS)})(?:-[2-9][0-9]*)?\.tsv$"
)


@dataclass(frozen=True)
class ValidationResult:
    rows: int
    prompts: tuple[str, ...]


def load_tsv(path: Path) -> list[list[str]]:
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"TSV is not valid UTF-8: {exc}") from exc

    if text.startswith("\ufeff"):
        raise ValueError("TSV must not contain a UTF-8 byte-order mark")
    if "\x00" in text:
        raise ValueError("TSV contains a NUL character")
    if "\r" in text:
        raise ValueError("TSV must use LF line endings and no physical field newlines")

    physical_lines = text.split("\n")
    if physical_lines and physical_lines[-1] == "":
        physical_lines.pop()
    if not physical_lines:
        return []

    rows: list[list[str]] = []
    for number, line in enumerate(physical_lines, start=1):
        if line.count("\t") != 3:
            raise ValueError(
                f"row {number} contains {line.count(chr(9))} tab separators; expected 3"
            )
        rows.append(line.split("\t"))
    return rows


def validate_tsv(path: Path, expected_count: int | None = None) -> ValidationResult:
    if not NATO_PATTERN.fullmatch(path.name):
        raise ValueError("TSV filename does not follow the NATO export sequence")
    rows = load_tsv(path)
    if not rows:
        raise ValueError("TSV contains no cards")

    prompts: list[str] = []
    for number, row in enumerate(rows, start=1):
        if len(row) != 4:
            raise ValueError(f"row {number} has {len(row)} fields; expected exactly 4")
        if not row[0].strip():
            raise ValueError(f"row {number} has an empty Prompt")
        prompts.append(row[0])

    if expected_count is not None and len(rows) != expected_count:
        raise ValueError(f"TSV has {len(rows)} rows; expected {expected_count}")

    return ValidationResult(rows=len(rows), prompts=tuple(prompts))


def load_report(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Validation Report JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Validation Report root must be an object")
    return data


def non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def validate_duplicate_outcomes(value: Any, policy: str) -> None:
    if not isinstance(value, list):
        raise ValueError("batch.duplicate_outcomes must be a list")
    allowed_outcomes = {
        "reject": {"rejected"},
        "merge": {"merged", "merged-with-conflicts"},
        "allow": {"kept"},
    }
    for outcome in value:
        if not isinstance(outcome, dict):
            raise ValueError("each duplicate outcome must be an object")
        non_empty_string(outcome.get("source_id"), "duplicate source_id")
        non_empty_string(outcome.get("prompt"), "duplicate prompt")
        if outcome.get("policy") != policy:
            raise ValueError("duplicate outcome policy does not match batch policy")
        if outcome.get("outcome") not in allowed_outcomes[policy]:
            raise ValueError("duplicate outcome is invalid for the configured policy")


def validate_report(path: Path, tsv_path: Path, tsv_result: ValidationResult) -> None:
    expected_report_name = tsv_path.stem + ".validation.json"
    if path.name != expected_report_name or path.parent.resolve() != tsv_path.parent.resolve():
        raise ValueError("Validation Report must be the TSV's same-directory companion")

    report = load_report(path)
    required_top = {
        "schema_version", "export_filename", "card_count", "language_profile",
        "batch", "cards", "rejected_sources",
    }
    if set(report) != required_top:
        raise ValueError("Validation Report has missing or unexpected top-level properties")
    if report["schema_version"] != 1:
        raise ValueError("Validation Report schema_version must be 1")
    if report["export_filename"] != tsv_path.name:
        raise ValueError("Validation Report export_filename does not match TSV")
    if report["card_count"] != tsv_result.rows:
        raise ValueError("Validation Report card_count does not match TSV rows")
    non_empty_string(report["language_profile"], "language_profile")

    batch = report["batch"]
    required_batch = {
        "adapter", "identity", "identity_scope", "finalized_by",
        "duplicate_policy", "duplicate_outcomes",
    }
    if not isinstance(batch, dict) or set(batch) != required_batch:
        raise ValueError("batch has missing or unexpected properties")
    non_empty_string(batch["adapter"], "batch.adapter")
    non_empty_string(batch["identity"], "batch.identity")
    if batch["identity_scope"] not in {"user", "user-and-conversation"}:
        raise ValueError("batch.identity_scope is invalid")
    if batch["finalized_by"] not in {"explicit", "inactivity"}:
        raise ValueError("batch.finalized_by is invalid")
    if batch["duplicate_policy"] not in DUPLICATE_POLICIES:
        raise ValueError("batch.duplicate_policy is invalid")
    validate_duplicate_outcomes(batch["duplicate_outcomes"], batch["duplicate_policy"])

    cards = report["cards"]
    if not isinstance(cards, list) or len(cards) != tsv_result.rows:
        raise ValueError("Validation Report cards must contain one item per TSV row")

    seen_rows: set[int] = set()
    for card in cards:
        required_card = {
            "row", "source_id", "source_type", "prompt", "provenance",
            "uncertain_fields", "review_required", "issues",
        }
        if not isinstance(card, dict) or set(card) != required_card:
            raise ValueError("each card has missing or unexpected properties")
        row = card["row"]
        if not isinstance(row, int) or not 1 <= row <= tsv_result.rows:
            raise ValueError("Validation Report card row is out of range")
        if row in seen_rows:
            raise ValueError(f"Validation Report duplicates row {row}")
        seen_rows.add(row)

        non_empty_string(card["source_id"], f"row {row} source_id")
        if card["source_type"] not in SOURCE_TYPES:
            raise ValueError(f"row {row} source_type is invalid")
        if card["prompt"] != tsv_result.prompts[row - 1]:
            raise ValueError(f"Validation Report prompt mismatch at row {row}")

        provenance = card["provenance"]
        if not isinstance(provenance, dict) or set(provenance) != set(ROLES):
            raise ValueError(f"row {row} provenance must contain all four Card Roles")
        invalid = {value for value in provenance.values() if value not in PROVENANCE}
        if invalid:
            raise ValueError(f"row {row} has invalid provenance values: {sorted(invalid)}")

        uncertain = card["uncertain_fields"]
        if not isinstance(uncertain, list) or any(field not in ROLES for field in uncertain):
            raise ValueError(f"row {row} uncertain_fields contains an invalid Card Role")
        if not isinstance(card["review_required"], bool):
            raise ValueError(f"row {row} review_required must be boolean")
        issues = card["issues"]
        if not isinstance(issues, list) or any(not isinstance(item, str) for item in issues):
            raise ValueError(f"row {row} issues must be a list of strings")

    if seen_rows != set(range(1, tsv_result.rows + 1)):
        raise ValueError("Validation Report does not cover every TSV row")

    rejected = report["rejected_sources"]
    if not isinstance(rejected, list):
        raise ValueError("rejected_sources must be a list")
    for item in rejected:
        if not isinstance(item, dict) or set(item) != {"source_id", "reason"}:
            raise ValueError("each rejected source must contain source_id and reason")
        non_empty_string(item["source_id"], "rejected source_id")
        non_empty_string(item["reason"], "rejected reason")


def validate_export(
    tsv_path: Path,
    report_path: Path,
    expected_count: int | None = None,
) -> ValidationResult:
    result = validate_tsv(tsv_path, expected_count)
    validate_report(report_path, tsv_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tsv", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    args = parser.parse_args()

    try:
        result = validate_export(args.tsv, args.report, args.expected_count)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"validation failed: {exc}\n")

    print(f"validated {result.rows} cards: {args.tsv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
