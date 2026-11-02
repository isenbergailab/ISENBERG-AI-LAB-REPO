#!/usr/bin/env python3
"""Deterministic core for batching and exporting Anki card candidates."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROLES = ("Prompt", "Answer", "Notes", "Example")
PROVENANCE = {"source", "reference", "generated"}
SOURCE_TYPES = {"text", "image", "list-item"}
DUPLICATE_POLICIES = {"reject", "merge", "allow"}
NATO = (
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf",
    "Hotel", "India", "Juliett", "Kilo", "Lima", "Mike", "November",
    "Oscar", "Papa", "Quebec", "Romeo", "Sierra", "Tango", "Uniform",
    "Victor", "Whiskey", "Xray", "Yankee", "Zulu",
)


class CoreError(ValueError):
    """A user-correctable core or configuration error."""


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def default_config_path() -> Path:
    override = os.environ.get("AUTOMATIC_ANKI_CONFIG")
    if override:
        return Path(override).expanduser()
    return default_hermes_home() / "automatic-anki-flashcard-creation" / "config.json"


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CoreError(f"file not found: {path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoreError(f"invalid JSON in {path}: {exc}") from exc


def resolve_path(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def resolve_profile(config: dict[str, Any], config_path: Path) -> tuple[dict[str, Any], Path]:
    profile_value = config.get("language_profile")
    if not isinstance(profile_value, str) or not profile_value.strip():
        raise CoreError("language_profile must be a non-empty string")

    candidate = Path(profile_value)
    if candidate.suffix:
        profile_path = resolve_path(profile_value, config_path)
    else:
        profile_path = skill_root() / "language-profiles" / f"{profile_value}.json"

    profile = load_json(profile_path)
    if not isinstance(profile, dict):
        raise CoreError("Language Profile root must be an object")
    return profile, profile_path


def validate_profile(profile: dict[str, Any]) -> None:
    for key in ("name", "target_language", "explanation_language"):
        if not isinstance(profile.get(key), str) or not profile[key].strip():
            raise CoreError(f"Language Profile {key} must be a non-empty string")
    roles = profile.get("roles")
    if not isinstance(roles, dict) or set(roles) != set(ROLES):
        raise CoreError("Language Profile roles must contain exactly four Card Roles")
    if any(not isinstance(roles[role], str) or not roles[role].strip() for role in ROLES):
        raise CoreError("every Language Profile role description must be non-empty")
    rules = profile.get("rules")
    if not isinstance(rules, list) or not rules or any(not isinstance(rule, str) for rule in rules):
        raise CoreError("Language Profile rules must be a non-empty list of strings")


def validate_config(config: dict[str, Any], profile: dict[str, Any]) -> None:
    validate_profile(profile)
    for key in ("target_language", "explanation_language"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise CoreError(f"config {key} must be a non-empty string")
    if config["target_language"] != profile["target_language"]:
        raise CoreError("config target_language does not match Language Profile")
    if config["explanation_language"] != profile["explanation_language"]:
        raise CoreError("config explanation_language does not match Language Profile")

    mapping = config.get("field_mapping")
    if not isinstance(mapping, dict) or set(mapping) != set(ROLES):
        raise CoreError("field_mapping must contain exactly four Card Roles")
    names = [mapping[role] for role in ROLES]
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise CoreError("field_mapping values must be non-empty strings")
    if len({name.casefold() for name in names}) != 4:
        raise CoreError("field_mapping values must be distinct")

    duplicate = config.get("duplicates")
    if not isinstance(duplicate, dict) or duplicate.get("policy") not in DUPLICATE_POLICIES:
        raise CoreError("duplicates.policy must be reject, merge, or allow")
    if not isinstance(duplicate.get("case_sensitive", False), bool):
        raise CoreError("duplicates.case_sensitive must be boolean")

    batch = config.get("batch")
    if not isinstance(batch, dict) or batch.get("explicit_export") is not True:
        raise CoreError("batch.explicit_export must be true")
    inactivity = batch.get("inactivity_seconds")
    if not isinstance(inactivity, int) or inactivity < 0:
        raise CoreError("batch.inactivity_seconds must be a non-negative integer")

    output = config.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("directory"), str):
        raise CoreError("output.directory must be a string")
    if output.get("naming") != "nato" or output.get("write_validation_report") is not True:
        raise CoreError("output must use NATO naming and write a Validation Report")

    storage = config.get("storage")
    if not isinstance(storage, dict) or not isinstance(storage.get("state_directory"), str):
        raise CoreError("storage.state_directory must be a string")
    if storage.get("identity_scope") not in {"user", "user-and-conversation"}:
        raise CoreError("storage.identity_scope is invalid")

    adapter = config.get("adapter")
    if not isinstance(adapter, dict) or adapter.get("type") not in {
        "hermes", "telegram", "discord", "custom"
    }:
        raise CoreError("adapter.type is invalid")


def load_runtime(config_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_json(config_path)
    if not isinstance(config, dict):
        raise CoreError("config root must be an object")
    profile, _ = resolve_profile(config, config_path)
    validate_config(config, profile)
    return config, profile


def identity_key(
    adapter: str,
    user_id: str,
    conversation_id: str,
    scope: str,
) -> str:
    if not adapter or not user_id or (scope == "user-and-conversation" and not conversation_id):
        raise CoreError("adapter, user_id, and required conversation_id must be non-empty")
    raw = f"{adapter}\x1f{user_id}"
    if scope == "user-and-conversation":
        raw += f"\x1f{conversation_id}"
    prefix = re.sub(r"[^a-z0-9-]+", "-", adapter.casefold()).strip("-") or "adapter"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def runtime_paths(
    config: dict[str, Any],
    config_path: Path,
    adapter: str,
    user_id: str,
    conversation_id: str,
) -> tuple[str, Path, Path]:
    scope = config["storage"]["identity_scope"]
    key = identity_key(adapter, user_id, conversation_id, scope)
    state_root = resolve_path(config["storage"]["state_directory"], config_path)
    output_root = resolve_path(config["output"]["directory"], config_path)
    return key, state_root / f"{key}.json", output_root / key


def empty_state(identity: str, adapter: str, user_id: str, conversation_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "identity": identity,
        "adapter": adapter,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "cards": [],
        "rejected_sources": [],
        "duplicate_outcomes": [],
    }


def load_state(path: Path, identity: str, adapter: str, user_id: str, conversation_id: str) -> dict[str, Any]:
    if not path.exists():
        return empty_state(identity, adapter, user_id, conversation_id)
    state = load_json(path)
    if not isinstance(state, dict) or state.get("identity") != identity:
        raise CoreError("batch state is invalid or belongs to another identity")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def normalize_prompt(prompt: str, case_sensitive: bool) -> str:
    normalized = " ".join(prompt.split())
    return normalized if case_sensitive else normalized.casefold()


def sanitize_plain(value: str) -> str:
    if not isinstance(value, str):
        raise CoreError("Card fields must be strings")
    if "\t" in value or "\r" in value or "\n" in value or "\x00" in value:
        raise CoreError("Card fields must not contain tabs, physical newlines, or NUL")
    return html.escape(value, quote=False)


def candidate_to_card(candidate: dict[str, Any]) -> dict[str, Any]:
    source_id = candidate.get("source_id")
    source_type = candidate.get("source_type")
    if not isinstance(source_id, str) or not source_id.strip():
        raise CoreError("candidate source_id must be non-empty")
    if source_type not in SOURCE_TYPES:
        raise CoreError(f"candidate source_type must be one of {sorted(SOURCE_TYPES)}")

    fields = candidate.get("fields")
    if not isinstance(fields, dict) or set(fields) != set(ROLES):
        raise CoreError("eligible candidate fields must contain exactly four Card Roles")

    example = fields["Example"]
    if not isinstance(example, list) or not example or any(not isinstance(line, str) for line in example):
        raise CoreError("Example must be a non-empty list of plain-text lines")

    sanitized = {
        "Prompt": sanitize_plain(fields["Prompt"]),
        "Answer": sanitize_plain(fields["Answer"]),
        "Notes": sanitize_plain(fields["Notes"]),
        "Example": "<br>".join(sanitize_plain(line) for line in example),
    }
    if not sanitized["Prompt"].strip():
        raise CoreError("Prompt must not be empty")

    provenance = candidate.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != set(ROLES):
        raise CoreError("candidate provenance must contain exactly four Card Roles")
    if any(value not in PROVENANCE for value in provenance.values()):
        raise CoreError("candidate provenance contains an invalid value")

    uncertain = candidate.get("uncertain_fields", [])
    if not isinstance(uncertain, list) or any(role not in ROLES for role in uncertain):
        raise CoreError("candidate uncertain_fields contains an invalid Card Role")
    issues = candidate.get("issues", [])
    if not isinstance(issues, list) or any(not isinstance(issue, str) for issue in issues):
        raise CoreError("candidate issues must be a list of strings")

    return {
        "source_id": source_id,
        "source_type": source_type,
        "fields": sanitized,
        "provenance": provenance,
        "uncertain_fields": uncertain,
        "review_required": bool(candidate.get("review_required", False) or uncertain),
        "issues": issues,
    }


def add_candidate(
    config_path: Path,
    adapter: str,
    user_id: str,
    conversation_id: str,
    candidate_path: Path,
) -> dict[str, Any]:
    config, _profile = load_runtime(config_path)
    identity, state_path, _output_dir = runtime_paths(
        config, config_path, adapter, user_id, conversation_id
    )
    state = load_state(state_path, identity, adapter, user_id, conversation_id)
    candidate = load_json(candidate_path)
    if not isinstance(candidate, dict):
        raise CoreError("candidate root must be an object")

    source_id = candidate.get("source_id")
    if not isinstance(source_id, str) or not source_id.strip():
        raise CoreError("candidate source_id must be non-empty")
    if candidate.get("source_type") not in SOURCE_TYPES:
        raise CoreError(f"candidate source_type must be one of {sorted(SOURCE_TYPES)}")

    if candidate.get("eligible") is not True:
        reason = candidate.get("rejection_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise CoreError("ineligible candidate requires rejection_reason")
        state["rejected_sources"].append({"source_id": source_id, "reason": reason})
        save_state(state_path, state)
        return {"status": "rejected", "source_id": source_id, "reason": reason}

    card = candidate_to_card(candidate)
    case_sensitive = config["duplicates"].get("case_sensitive", False)
    normalized = normalize_prompt(card["fields"]["Prompt"], case_sensitive)
    matches = [
        item for item in state["cards"]
        if normalize_prompt(item["fields"]["Prompt"], case_sensitive) == normalized
    ]
    policy = config["duplicates"]["policy"]

    if not matches or policy == "allow":
        state["cards"].append(card)
        if matches:
            state["duplicate_outcomes"].append({
                "source_id": source_id,
                "prompt": card["fields"]["Prompt"],
                "policy": "allow",
                "outcome": "kept",
            })
        status = "added"
    elif policy == "reject":
        state["duplicate_outcomes"].append({
            "source_id": source_id,
            "prompt": card["fields"]["Prompt"],
            "policy": "reject",
            "outcome": "rejected",
        })
        status = "duplicate-rejected"
    else:
        existing = matches[0]
        conflicts: list[str] = []
        for role in ROLES:
            old = existing["fields"][role]
            new = card["fields"][role]
            if not old and new:
                existing["fields"][role] = new
                existing["provenance"][role] = card["provenance"][role]
            elif old != new:
                conflicts.append(role)
        existing["uncertain_fields"] = sorted(
            set(existing.get("uncertain_fields", [])) | set(card["uncertain_fields"])
        )
        existing["issues"] = list(existing.get("issues", [])) + list(card["issues"])
        if conflicts:
            existing["review_required"] = True
            existing["issues"].append(
                "Duplicate merge retained the first value for conflicting roles: "
                + ", ".join(conflicts)
            )
        state["duplicate_outcomes"].append({
            "source_id": source_id,
            "prompt": card["fields"]["Prompt"],
            "policy": "merge",
            "outcome": "merged-with-conflicts" if conflicts else "merged",
        })
        status = "duplicate-merged"

    save_state(state_path, state)
    return {
        "status": status,
        "identity": identity,
        "card_count": len(state["cards"]),
        "rejected_count": len(state["rejected_sources"]),
    }


def filename_for(index: int) -> str:
    zero = index - 1
    cycle, word = divmod(zero, len(NATO))
    suffix = "" if cycle == 0 else f"-{cycle + 1}"
    return f"{NATO[word]}{suffix}.tsv"


def reserve_pair(directory: Path) -> tuple[Path, Path, int, int]:
    directory.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        tsv_path = directory / filename_for(index)
        report_path = tsv_path.with_suffix(".validation.json")
        try:
            tsv_fd = os.open(tsv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            index += 1
            continue
        try:
            report_fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            os.close(tsv_fd)
            tsv_path.unlink(missing_ok=True)
            index += 1
            continue
        return tsv_path, report_path, tsv_fd, report_fd


def export_batch(
    config_path: Path,
    adapter: str,
    user_id: str,
    conversation_id: str,
    finalized_by: str = "explicit",
) -> dict[str, Any]:
    config, profile = load_runtime(config_path)
    identity, state_path, output_dir = runtime_paths(
        config, config_path, adapter, user_id, conversation_id
    )
    state = load_state(state_path, identity, adapter, user_id, conversation_id)
    cards = state.get("cards", [])
    if not cards:
        raise CoreError("current Batch contains no cards")
    if finalized_by not in {"explicit", "inactivity"}:
        raise CoreError("finalized_by must be explicit or inactivity")
    if finalized_by == "inactivity" and config["batch"]["inactivity_seconds"] <= 0:
        raise CoreError("inactivity finalization is disabled by configuration")

    tsv_path, report_path, tsv_fd, report_fd = reserve_pair(output_dir)
    try:
        tsv_text = "".join(
            "\t".join(card["fields"][role] for role in ROLES) + "\n"
            for card in cards
        )
        with os.fdopen(tsv_fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(tsv_text)

        report_cards = []
        for row, card in enumerate(cards, start=1):
            report_cards.append({
                "row": row,
                "source_id": card["source_id"],
                "source_type": card["source_type"],
                "prompt": card["fields"]["Prompt"],
                "provenance": card["provenance"],
                "uncertain_fields": card["uncertain_fields"],
                "review_required": card["review_required"],
                "issues": card["issues"],
            })

        report = {
            "schema_version": 1,
            "export_filename": tsv_path.name,
            "card_count": len(cards),
            "language_profile": profile["name"],
            "batch": {
                "adapter": adapter,
                "identity": identity,
                "identity_scope": config["storage"]["identity_scope"],
                "finalized_by": finalized_by,
                "duplicate_policy": config["duplicates"]["policy"],
                "duplicate_outcomes": state.get("duplicate_outcomes", []),
            },
            "cards": report_cards,
            "rejected_sources": state.get("rejected_sources", []),
        }
        with os.fdopen(report_fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        try:
            os.close(tsv_fd)
        except OSError:
            pass
        try:
            os.close(report_fd)
        except OSError:
            pass
        tsv_path.unlink(missing_ok=True)
        report_path.unlink(missing_ok=True)
        raise

    from validate_export import validate_export

    try:
        validate_export(tsv_path, report_path, expected_count=len(cards))
    except Exception:
        tsv_path.unlink(missing_ok=True)
        report_path.unlink(missing_ok=True)
        raise
    state_path.unlink(missing_ok=True)
    return {
        "status": "exported",
        "card_count": len(cards),
        "tsv": str(tsv_path.resolve()),
        "report": str(report_path.resolve()),
        "media": [
            f"MEDIA:{tsv_path.resolve()}",
            f"MEDIA:{report_path.resolve()}",
        ],
    }


def initialize_config(home: Path, force: bool = False) -> Path:
    destination = home.expanduser().resolve() / "automatic-anki-flashcard-creation" / "config.json"
    if destination.exists() and not force:
        raise CoreError(f"config already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    example = load_json(skill_root() / "config" / "example.json")
    destination.write_text(json.dumps(example, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination.parent / "state").mkdir(exist_ok=True)
    (destination.parent / "exports").mkdir(exist_ok=True)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create a profile-local example config")
    init.add_argument("--hermes-home", type=Path, default=default_hermes_home())
    init.add_argument("--force", action="store_true")

    check = subparsers.add_parser("check-config", help="validate config and Language Profile")
    check.add_argument("--config", type=Path, default=default_config_path())

    for name in ("add", "export", "reset"):
        command = subparsers.add_parser(name)
        command.add_argument("--config", type=Path, default=default_config_path())
        command.add_argument("--adapter", required=True)
        command.add_argument("--user-id", required=True)
        command.add_argument("--conversation-id", default="")
        if name == "add":
            command.add_argument("--source", type=Path, required=True)
        if name == "export":
            command.add_argument(
                "--finalized-by",
                choices=("explicit", "inactivity"),
                default="explicit",
            )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "init":
            result: Any = {"config": str(initialize_config(args.hermes_home, args.force))}
        elif args.command == "check-config":
            config, profile = load_runtime(args.config.resolve())
            result = {
                "status": "valid",
                "target_language": config["target_language"],
                "language_profile": profile["name"],
            }
        elif args.command == "add":
            result = add_candidate(
                args.config.resolve(), args.adapter, args.user_id,
                args.conversation_id, args.source.resolve()
            )
        elif args.command == "export":
            result = export_batch(
                args.config.resolve(), args.adapter, args.user_id,
                args.conversation_id, args.finalized_by
            )
        else:
            config, _profile = load_runtime(args.config.resolve())
            identity, state_path, _output = runtime_paths(
                config, args.config.resolve(), args.adapter,
                args.user_id, args.conversation_id
            )
            state_path.unlink(missing_ok=True)
            result = {"status": "reset", "identity": identity}
    except (CoreError, OSError) as exc:
        parser.exit(1, f"error: {exc}\n")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
