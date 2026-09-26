"""Guardrail checks for the Isenberg AI Lab repo.

Runs locally (python scripts/check_repo.py) and in GitHub Actions on every PR.
Exit code 1 on any ERROR, so the PR cannot merge until it is fixed.
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
errors, warnings = [], []

REQUIRED = ["name", "slug", "lead", "status", "summary", "data_tier",
            "environment", "sandbox", "approval_actions", "unattended_runs",
            "spend_cap_usd", "kill_switch", "logs", "teardown"]
ENUMS = {
    "status": {"proposed", "active", "shipped", "paused", "failed", "archived"},
    "data_tier": {"public", "synthetic", "personal", "restricted"},
    "environment": {"docker", "vm", "cloud-workspace"},
    "teardown": {"not-started", "done", "n/a"},
}
SANDBOX_KEYS = ["isolated", "disposable", "fake_inputs", "capped", "watched"]

# Common key shapes. Not exhaustive; GitHub secret scanning is the backstop.
SECRET_PATTERNS = {
    "OpenAI/OpenRouter-style key": r"sk-(or-v1-|proj-|ant-)?[A-Za-z0-9_\-]{20,}",
    "GitHub token": r"gh[pousr]_[A-Za-z0-9]{30,}",
    "Hugging Face token": r"hf_[A-Za-z0-9]{30,}",
    "Google API key": r"AIza[0-9A-Za-z_\-]{35}",
    "AWS access key": r"AKIA[0-9A-Z]{16}",
    "Private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}
SKIP_DIRS = {".git", "site", "node_modules", "__pycache__", ".venv", "venv"}
TEXT_EXT = {".py", ".js", ".ts", ".md", ".txt", ".json", ".yml", ".yaml",
            ".toml", ".ipynb", ".sh", ".ps1", ".cfg", ".ini", ".env", ""}


def rel(p):
    return p.relative_to(ROOT).as_posix()


def check_projects():
    for folder in sorted(p for p in (ROOT / "projects").glob("*") if p.is_dir()):
        readme = folder / "README.md"
        if not readme.exists():
            errors.append(f"{rel(folder)}: missing README.md with project header")
            continue
        text = readme.read_text(encoding="utf-8")
        if not text.startswith("---"):
            errors.append(f"{rel(readme)}: missing YAML header (see templates/)")
            continue
        try:
            h = yaml.safe_load(text.split("---", 2)[1]) or {}
        except yaml.YAMLError as e:
            errors.append(f"{rel(readme)}: header is not valid YAML ({e})")
            continue
        where = rel(readme)
        for key in REQUIRED:
            if h.get(key) in (None, ""):
                errors.append(f"{where}: missing '{key}'")
        for key, allowed in ENUMS.items():
            if h.get(key) and h[key] not in allowed:
                errors.append(f"{where}: '{key}' must be one of {sorted(allowed)}")
        if h.get("slug") and h["slug"] != folder.name:
            errors.append(f"{where}: slug '{h['slug']}' must match folder '{folder.name}'")
        sandbox = h.get("sandbox") or {}
        gaps = [k for k in SANDBOX_KEYS if sandbox.get(k) is not True]
        reviewed = bool(h.get("officer_review"))
        if gaps and not reviewed:
            errors.append(f"{where}: sandbox not met ({', '.join(gaps)}) and no officer_review")
        if h.get("unattended_runs") and not reviewed:
            errors.append(f"{where}: unattended_runs needs officer_review")
        if h.get("data_tier") == "restricted":
            errors.append(f"{where}: restricted data needs written permission; "
                          "an officer must approve this PR manually")
        cap, spent = h.get("spend_cap_usd") or 0, h.get("spend_to_date_usd") or 0
        if cap and spent > cap:
            warnings.append(f"{where}: spend ${spent} is over cap ${cap}")
        if h.get("status") in ("failed", "archived") and h.get("teardown") != "done":
            warnings.append(f"{where}: teardown not done")
        if list(folder.rglob(".env")):
            errors.append(f"{rel(folder)}: .env file committed; delete it and revoke the key")


def check_skills():
    skills = ROOT / "skills"
    if not skills.exists():
        return
    for folder in sorted(p for p in skills.glob("*") if p.is_dir()):
        doc = next((folder / n for n in ("SKILL.md", "README.md")
                    if (folder / n).exists()), None)
        if doc is None:
            warnings.append(f"{rel(folder)}: no SKILL.md or README.md")
        elif "## guardrails" not in doc.read_text(encoding="utf-8").lower():
            warnings.append(f"{rel(doc)}: add a '## Guardrails' section "
                            "(data tier, approval actions, how to stop it)")


def check_secrets():
    for path in ROOT.rglob("*"):
        if not path.is_file() or SKIP_DIRS & set(path.parts):
            continue
        if path.name == ".env.example" or path.suffix not in TEXT_EXT:
            continue
        if path.name == ".env" or path.name.startswith(".env."):
            errors.append(f"{rel(path)}: env file must not be committed")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if re.search(pattern, text):
                errors.append(f"{rel(path)}: looks like a {label}. Remove it, "
                              "then revoke the key today.")


if __name__ == "__main__":
    check_projects()
    check_skills()
    check_secrets()
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    if errors:
        print(f"\n{len(errors)} error(s). See AGENTS.md for the rules.")
        sys.exit(1)
    print(f"Guardrail checks passed ({len(warnings)} warning(s)).")
