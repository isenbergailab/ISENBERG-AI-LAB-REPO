"""Draft the monthly report from project README headers.

Usage:  python scripts/build_report.py 2026-10
Output: docs/reports/2026-10.md  (a draft; an officer edits and approves it)

Reads the YAML header of every projects/*/README.md, then fills the
<!-- AUTO:... --> markers in templates/MONTHLY_REPORT_TEMPLATE.md.
Refuses to overwrite an existing report so human edits are never lost.
"""
import sys
import calendar
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/isenbergailab/ISENBERG-AI-LAB-REPO/blob/main/"
SANDBOX_KEYS = ["isolated", "disposable", "fake_inputs", "capped", "watched"]


def load_projects():
    projects = []
    for readme in sorted(ROOT.glob("projects/*/README.md")):
        text = readme.read_text(encoding="utf-8")
        if not text.startswith("---"):
            print(f"WARN no header: {readme.relative_to(ROOT)}")
            continue
        header = yaml.safe_load(text.split("---", 2)[1]) or {}
        header["_path"] = readme.relative_to(ROOT).as_posix()
        projects.append(header)
    return projects


def in_period(value, period):
    return value is not None and str(value).startswith(period)


def link(p):
    return f"[{p.get('name', '?')}]({REPO}{p['_path']})"


def table(rows, cols):
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def flags(p):
    """Return guardrail problems that need an officer's attention."""
    issues = []
    sandbox = p.get("sandbox") or {}
    missing = [k for k in SANDBOX_KEYS if not sandbox.get(k)]
    if missing and not p.get("officer_review"):
        issues.append("sandbox gap, no review: " + ", ".join(missing))
    if p.get("unattended_runs") and not p.get("officer_review"):
        issues.append("unattended without review")
    if p.get("data_tier") == "restricted":
        issues.append("restricted data, confirm written permission")
    cap, spent = p.get("spend_cap_usd") or 0, p.get("spend_to_date_usd") or 0
    if cap and spent > cap:
        issues.append(f"over cap (${spent} of ${cap})")
    if p.get("status") in ("archived", "failed") and p.get("teardown") != "done":
        issues.append("teardown not done")
    return issues


def build(period):
    year, month = map(int, period.split("-"))
    projects = load_projects()

    shipped = [p for p in projects if in_period(p.get("shipped"), period)]
    active = [p for p in projects if p.get("status") == "active"]
    reported = {id(p): p for p in shipped + active}.values()

    shipped_md = "\n".join(
        f"- {link(p)}: {p.get('summary', '')} (lead: {p.get('lead', '?')})"
        for p in shipped) or "Nothing shipped this month."

    active_md = "\n".join(
        f"- {link(p)}: {p.get('summary', '')}" for p in active
    ) or "No active projects."

    rows = []
    for p in reported:
        sandbox = p.get("sandbox") or {}
        met = sum(bool(sandbox.get(k)) for k in SANDBOX_KEYS)
        rows.append([
            p.get("name"), p.get("data_tier"), p.get("environment"),
            f"{met}/5", ", ".join(p.get("approval_actions") or []) or "none",
            f"${p.get('spend_to_date_usd', 0)} / ${p.get('spend_cap_usd', 0)}",
            "; ".join(flags(p)) or "ok",
        ])
    guard_md = table(rows, ["Project", "Data tier", "Runs in", "Sandbox",
                            "Approval actions", "Spend / cap", "Flags"]) \
        if rows else "No projects to check."

    incidents = [
        (p, i) for p in projects for i in (p.get("incidents") or [])
        if in_period(i.get("date"), period)]
    incident_md = "\n".join(
        f"- {i.get('date')}, {p.get('name')}: {i.get('summary', '')}"
        + (f" ([write-up]({REPO}{i['writeup']}))" if i.get("writeup") else "")
        for p, i in incidents) or "No incidents reported."

    total_spend = sum(p.get("spend_to_date_usd") or 0 for p in projects)
    metrics_md = (f"- Projects on board: {len(projects)} "
                  f"({len(active)} active, {len(shipped)} shipped this month)\n"
                  f"- Total API spend to date: ${total_spend:.2f}")

    text = (ROOT / "templates/MONTHLY_REPORT_TEMPLATE.md").read_text(encoding="utf-8")
    text = (text.replace("MONTH YEAR", f"{calendar.month_name[month]} {year}")
                .replace("YYYY-MM", period)
                .replace("<!-- AUTO:SHIPPED -->", shipped_md)
                .replace("<!-- AUTO:ACTIVE -->", active_md)
                .replace("<!-- AUTO:GUARDRAILS -->", guard_md)
                .replace("<!-- AUTO:INCIDENTS -->", incident_md)
                .replace("<!-- AUTO:METRICS -->", metrics_md))

    out = ROOT / f"docs/reports/{period}.md"
    if out.exists():
        sys.exit(f"{out.relative_to(ROOT)} already exists. Not overwriting.")
    out.write_text(text, encoding="utf-8")
    print(f"Draft written: {out.relative_to(ROOT)}")
    for p in reported:
        for issue in flags(p):
            print(f"FLAG {p.get('name')}: {issue}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        period = sys.argv[1]
    else:  # default: last month
        today = date.today()
        y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        period = f"{y}-{m:02d}"
    build(period)
