"""Deterministic drafts; every claim remains tied to supplied evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE = re.compile(r"(?<!\w)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}(?!\w)")
HANDLE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{2,}")
QUOTE = re.compile(r"[\"“”‘’]")
SOURCE_KINDS = {
    "repo_metadata", "merged_pr", "approved_summary", "official_source", "president_preview"
}
SOURCE_ORDER = {kind: index for index, kind in enumerate(
    ("repo_metadata", "merged_pr", "approved_summary", "official_source", "president_preview")
)}
LIFECYCLE = ("draft", "awaiting_approval", "approved", "scheduled", "published", "skipped", "archived")


def _clean(value: str, *, allow_url: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("Copy must be text")
    value = " ".join(value.split())
    if not value or len(value) > 800:
        raise ValueError("Copy is empty or too long")
    if EMAIL.search(value) or PHONE.search(value) or HANDLE.search(value) or QUOTE.search(value):
        raise ValueError("Copy contains personal information or quotation marks")
    if not allow_url and ("http://" in value or "https://" in value):
        raise ValueError("URLs belong in sources")
    return value


def _sources(brief: dict) -> list[dict]:
    sources = brief.get("sources", [])
    if not isinstance(sources, list) or not sources:
        raise ValueError("At least one source is required")
    result = []
    for source in sources:
        if source.get("kind") not in SOURCE_KINDS:
            raise ValueError("Unsupported source kind")
        url = source.get("url", "")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("Sources need HTTPS URLs")
        result.append({"kind": source["kind"], "url": url, "claim": _clean(source["claim"])})
    return sorted(result, key=lambda source: SOURCE_ORDER[source["kind"]])


def _short(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1].rsplit(" ", 1)[0]
    return clipped + "…"


def _variant(seed: str, choices: tuple[str, ...]) -> str:
    number = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    return choices[number % len(choices)]


def _id(kind: str, label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:32]
    return f"{kind}-{slug}-{hashlib.sha256((kind + label).encode()).hexdigest()[:8]}"


def build_campaign(kind: str, brief: dict, *, model_hook: str = "") -> dict:
    """Return only draft artifacts; no channel writes occur here."""
    if kind not in {"weekly", "merge", "monthly", "recap"}:
        raise ValueError("Unsupported campaign kind")
    sources = _sources(brief)
    hook = _clean(model_hook) if model_hook else ""
    title = _clean(brief.get("title", ""))
    label = _clean(brief.get("label", brief.get("date", brief.get("month", title))))
    campaign_id = _id(kind, label)
    lead = hook or _variant(campaign_id, (
        "See what the Lab is exploring.",
        "Build, test, and discuss together.",
        "Follow the latest Lab work.",
    ))
    if kind == "weekly":
        day = _clean(brief["date"])
        topics = [_clean(item) for item in brief.get("topics", [])][:4]
        if not topics:
            topics = ["Member demos, AI news, and open discussion"]
        topic_line = "; ".join(topics)
        factual = f"Tuesday, {day}, 5:15–6 PM. Possible topics: {topic_line}."
        call = "Join Slack for the room and meeting link."
        slack = (f"Isenberg AI Lab meets Tuesday, {day}, 5:15–6 PM. "
                 f"Possible topics: {topic_line}. Demos are optional. Bring an idea, question, or experiment.")
        story = f"Tomorrow: Isenberg AI Lab · 5:15–6 PM · {topics[0]}"
        card_title = "This week at the Lab"
        card_subtitle = f"Tuesday · 5:15–6 PM · {topics[0]}"
    elif kind == "merge":
        if brief.get("merged") is not True:
            raise ValueError("Project posts require merged changes")
        if not any(source["kind"] == "merged_pr" for source in sources):
            raise ValueError("Project posts require merged PR evidence")
        summary = _clean(brief["summary"])
        factual = f"{title}. {summary}"
        call = "See the merged change in the Lab repository."
        slack = f"Merged in the Lab repository: {title}. {summary}"
        story = ""
        card_title = "New in the Lab repo"
        card_subtitle = title
    elif kind == "recap":
        if not any(source["kind"] == "approved_summary" for source in sources):
            raise ValueError("Recaps require a president-approved summary")
        summary = _clean(brief["summary"])
        factual = f"This week at Isenberg AI Lab: {summary}"
        call = "Explore verified Lab work in the repository."
        slack = f"This week's approved Lab takeaway: {summary}"
        story = ""
        card_title = "From this week"
        card_subtitle = summary
    else:
        sections = {}
        for key in ("shipped_projects", "active_projects", "meeting_takeaways", "guardrail_activity", "incidents_failures"):
            values = brief.get(key, [])
            if not isinstance(values, list):
                raise ValueError(f"{key} must be a list")
            sections[key] = [_clean(item) for item in values]
        month = _clean(brief["month"])
        factual = f"{month} at Isenberg AI Lab. " + (
            f"Shipped: {sections['shipped_projects'][0]}." if sections["shipped_projects"] else "Projects remain in progress."
        )
        call = "Read the full monthly report on Substack."
        slack = f"The {month} Isenberg AI Lab report is ready. Read it on Substack after publication."
        story = ""
        card_title = "Monthly Lab report"
        card_subtitle = month
    linkedin = f"{lead}\n\n{factual}\n\n{call}"
    instagram = f"{lead}\n\n{factual}\n\n{call}\n\n#IsenbergAILab #UMassAmherst"
    x = _short(f"{factual} {call}", 280)
    if len(x) > 280:
        raise ValueError("X copy exceeds 280 characters")
    report = ""
    if kind == "monthly":
        headings = (
            ("Shipped projects", "shipped_projects"),
            ("Active projects", "active_projects"),
            ("Meeting takeaways", "meeting_takeaways"),
            ("Guardrail activity", "guardrail_activity"),
            ("Incidents and failures", "incidents_failures"),
        )
        report = f"# {month} at Isenberg AI Lab\n\n"
        report += "## Month summary\n\n" + factual + "\n\n"
        for heading, key in headings:
            values = sections[key]
            report += f"## {heading}\n\n" + ("\n".join(f"- {v}" for v in values) if values else "Nothing verified for this section.") + "\n\n"
        public_sources = [s for s in sources if s["kind"] in {"repo_metadata", "merged_pr", "official_source"}]
        report += "## Sources\n\n" + ("\n".join(f"- {s['kind']}: {s['url']}" for s in public_sources)
                                         if public_sources else "Reviewed internal Lab records.") + "\n"
    return {
        "id": campaign_id, "kind": kind, "title": title, "label": label,
        "state": "draft", "sources": sources, "created_on": date.today().isoformat(),
        "copy": {"linkedin": linkedin, "instagram": instagram, "x": x, "slack": slack,
                 "instagram_story": story, "substack": report},
        "card": {"title": card_title, "subtitle": card_subtitle},
        "schedule": {
            "weekly": "Monday evening announcement; Tuesday afternoon story and Slack reminder; Wednesday verified recap",
            "merge": "After merged change and presidential approval",
            "recap": "Wednesday after a president-approved meeting summary",
            "monthly": "Draft three days early; publish final business day after presidential approval",
        }[kind],
        "approval": {"owner_role": "president", "approved_by": "", "approved_at": "", "deadline": brief.get("approval_deadline", "")},
        "publication": {"instagram": "", "linkedin": "", "x": "", "substack": "", "slack": ""},
    }


def write_package(campaign: dict, parent: Path, assets: Path) -> Path:
    from .render import render_card

    folder = parent / campaign["id"]
    if folder.exists():
        raise FileExistsError(f"Campaign already exists: {folder}")
    folder.mkdir(parents=True)
    (folder / "campaign.json").write_text(json.dumps(campaign, indent=2, ensure_ascii=False), encoding="utf-8")
    copy = campaign["copy"]
    for channel, filename in (("instagram", "instagram-caption.txt"), ("linkedin", "linkedin-post.txt"),
                              ("slack", "slack-post.txt"), ("instagram_story", "instagram-story.txt"),
                              ("substack", "substack-report.md")):
        if copy[channel]:
            (folder / filename).write_text(copy[channel] + "\n", encoding="utf-8")
    (folder / "x-post.txt").write_text(copy["x"] + "\n", encoding="utf-8")
    (folder / "x-alt-text.txt").write_text(f"Branded Isenberg AI Lab card: {campaign['card']['title']}. {campaign['card']['subtitle']}.\n", encoding="utf-8")
    (folder / "x-sources.md").write_text("\n".join(f"- {s['kind']}: {s['url']} — {s['claim']}" for s in campaign["sources"]) + "\n", encoding="utf-8")
    (folder / "posting-checklist.md").write_text(
        "# President posting checklist\n\n- [ ] Review every claim and image.\n- [ ] Approve in private Slack channel.\n"
        "- [ ] Schedule approved Instagram and LinkedIn posts natively.\n- [ ] Publish X manually.\n"
        "- [ ] Copy X permalink into campaign ledger.\n- [ ] Mark published only after confirming every intended channel.\n\n"
        f"Recommended window: {campaign['schedule']}\n", encoding="utf-8")
    render_card(campaign["card"], folder / "instagram-card.png", assets, (1080, 1350), campaign["id"])
    render_card(campaign["card"], folder / "x-image.png", assets, (1200, 675), campaign["id"])
    if campaign["kind"] == "weekly":
        render_card(campaign["card"], folder / "instagram-story.png", assets, (1080, 1920), campaign["id"])
    return folder
