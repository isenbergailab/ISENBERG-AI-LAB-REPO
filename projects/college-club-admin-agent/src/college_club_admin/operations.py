"""Low-frequency private jobs. Dates use America/New_York."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .campaigns import build_campaign, write_package
from .drive import upload_package
from .ledger import save_campaign, transition
from .model import draft_hook
from .slack import monday_thread, request_approval, _post

NY = ZoneInfo("America/New_York")
WEBSITE = "https://isenbergailab.netlify.app"


def _sheet():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    credentials = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=credentials, cache_discovery=False).spreadsheets().values()


def _job(key: str):
    rows = _sheet().get(spreadsheetId=os.environ["CAMPAIGN_SHEET_ID"], range="Jobs!A:C").execute().get("values", [])
    for row in rows:
        if row and row[0] == key:
            return row[1] if len(row) > 1 else ""
    return ""


def _record_job(key: str, value: str):
    api = _sheet()
    api.append(spreadsheetId=os.environ["CAMPAIGN_SHEET_ID"], range="Jobs!A:C", valueInputOption="RAW",
               body={"values": [[key, value, datetime.now(timezone.utc).isoformat()]]}).execute()


def _replies(ts: str) -> list[str]:
    response = requests.get("https://slack.com/api/conversations.replies",
                            headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
                            params={"channel": os.environ["SLACK_MEMBER_CHANNEL_ID"], "ts": ts, "limit": 100}, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "Slack replies unavailable"))
    return [message.get("text", "").lower() for message in data.get("messages", [])[1:]]


def _categories(messages: list[str]) -> list[str]:
    joined = " ".join(messages)
    topics = []
    if any(word in joined for word in ("demo", "show", "prototype")):
        topics.append("optional demos")
    if any(word in joined for word in ("news", "release", "model")):
        topics.append("AI news")
    if any(word in joined for word in ("failed", "error", "stuck", "problem")):
        topics.append("troubleshooting")
    topics.append("open discussion")
    return topics[:4]


def _draft_and_route(kind: str, brief: dict, drafts: Path, assets: Path) -> str:
    campaign = build_campaign(kind, brief)
    hook = draft_hook({"kind": kind, "title": campaign["title"],
                       "claims": [source["claim"] for source in campaign["sources"]]})
    campaign = build_campaign(kind, brief, model_hook=hook)
    campaign = transition(campaign, "awaiting_approval")
    folder = write_package(campaign, drafts, assets)
    campaign["private_package_url"] = upload_package(folder)
    save_campaign(folder, campaign)
    request_approval(campaign)
    return campaign["id"]


def _meeting_brief(now: datetime, topics: list[str]) -> dict:
    tuesday = now.date() + timedelta(days=1)
    deadline = now.replace(hour=16, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return {"title": "Weekly Lab meeting", "label": tuesday.isoformat(),
            "date": f"{tuesday:%B} {tuesday.day}, {tuesday.year}", "topics": topics,
            "approval_deadline": deadline.isoformat(),
            "sources": [{"kind": "official_source", "url": WEBSITE, "claim": "Weekly meetings are Tuesdays, 5:15–6 PM"}]}


def _campaign_row(campaign_id: str):
    rows = _sheet().get(spreadsheetId=os.environ["CAMPAIGN_SHEET_ID"], range="Campaigns!A:O").execute().get("values", [])
    for index, row in enumerate(rows[1:], start=2):
        if row and row[0] == campaign_id:
            return index, row
    return None, None


def _set_state(row_number: int, state: str):
    api = _sheet()
    api.update(spreadsheetId=os.environ["CAMPAIGN_SHEET_ID"], range=f"Campaigns!D{row_number}",
               valueInputOption="RAW", body={"values": [[state]]}).execute()
    api.update(spreadsheetId=os.environ["CAMPAIGN_SHEET_ID"], range=f"Campaigns!F{row_number}",
               valueInputOption="RAW", body={"values": [[datetime.now(timezone.utc).isoformat()]]}).execute()


def weekly_tick(now: datetime, drafts: Path, assets: Path) -> str:
    local = now.astimezone(NY)
    if local.weekday() == 0 and local.hour == 9:
        key = f"topic-{local.date()}"
        if _job(key): return "topic thread already posted"
        result = monday_thread(f"{(local.date() + timedelta(days=1)):%B} {(local.date() + timedelta(days=1)).day}")
        _record_job(key, result["ts"])
        return "topic thread posted"
    if local.weekday() == 0 and local.hour == 12:
        key = f"weekly-{local.date()}"
        if _job(key): return "weekly draft already routed"
        thread = _job(f"topic-{local.date()}")
        if not thread: return "no Monday topic thread; skipped"
        campaign_id = _draft_and_route("weekly", _meeting_brief(local, _categories(_replies(thread))), drafts, assets)
        _record_job(key, campaign_id)
        return f"weekly draft routed: {campaign_id}"
    if local.weekday() == 0 and local.hour >= 16:
        campaign_id = _job(f"weekly-{local.date()}")
        if not campaign_id: return "no campaign to expire"
        row_number, row = _campaign_row(campaign_id)
        if row and row[3] == "awaiting_approval":
            _set_state(row_number, "skipped")
            return "unapproved campaign skipped"
    if local.weekday() == 1 and local.hour == 14:
        monday = local.date() - timedelta(days=1)
        key = f"reminder-{local.date()}"
        if _job(key): return "reminder already posted"
        campaign_id = _job(f"weekly-{monday}")
        if not campaign_id: return "no approved campaign"
        _, row = _campaign_row(campaign_id)
        if not row or row[3] not in ("approved", "scheduled", "published"):
            return "campaign unapproved; reminder skipped"
        _post(os.environ["SLACK_MEMBER_CHANNEL_ID"],
              "Isenberg AI Lab meets today, 5:15–6 PM. Demos are optional. Bring a question, idea, or experiment.")
        _record_job(key, campaign_id)
        return "Slack reminder posted"
    if local.weekday() == 2 and local.hour == 12:
        meeting_date = (local.date() - timedelta(days=1)).isoformat()
        key = f"recap-{meeting_date}"
        if _job(key): return "recap already routed"
        from .drive import _api, _children
        drive = _api()
        for item in _children(drive, os.environ["DRIVE_APPROVED_SUMMARIES_FOLDER_ID"]):
            if not item["name"].endswith(".json"):
                continue
            summary = json.loads(drive.files().get_media(fileId=item["id"]).execute().decode("utf-8"))
            if summary.get("meeting_date") != meeting_date or summary.get("approved") is not True:
                continue
            takeaway = summary.get("public_takeaway", "")
            if not takeaway:
                continue
            brief = {"title": "Weekly Lab recap", "label": meeting_date, "summary": takeaway,
                     "sources": [{"kind": "approved_summary",
                                  "url": f"https://drive.google.com/file/d/{item['id']}/view",
                                  "claim": "President-approved weekly takeaway"}]}
            campaign_id = _draft_and_route("recap", brief, drafts, assets)
            _record_job(key, campaign_id)
            return f"Wednesday recap routed: {campaign_id}"
        return "no approved summary; recap skipped"
    return "no weekly action due"


def merged_event(payload: dict, drafts: Path, assets: Path) -> str:
    if payload.get("merged") is not True:
        return "unmerged change skipped"
    url = payload.get("url", "")
    if not url.startswith("https://github.com/isenbergailab/ISENBERG-AI-LAB-REPO/pull/"):
        raise ValueError("Unexpected repository URL")
    title = payload.get("title", "")
    brief = {"title": title, "label": f"pr-{payload['number']}", "merged": True,
             "summary": "A merged change is ready for review in the Lab repository.",
             "sources": [{"kind": "merged_pr", "url": url, "claim": title}]}
    return _draft_and_route("merge", brief, drafts, assets)


def monthly_brief(year: int, month: int) -> dict:
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    next_month = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=timezone.utc)
    response = requests.get("https://api.github.com/repos/isenbergailab/ISENBERG-AI-LAB-REPO/pulls",
                            params={"state": "closed", "per_page": 100, "sort": "updated", "direction": "desc"}, timeout=20)
    response.raise_for_status()
    merged = [item for item in response.json() if item.get("merged_at") and
              first <= datetime.fromisoformat(item["merged_at"].replace("Z", "+00:00")) < next_month]
    sources = [{"kind": "merged_pr", "url": item["html_url"], "claim": item["title"]} for item in merged]
    from .drive import _api, _children
    drive = _api()
    takeaways, guardrails, incidents = [], [], []
    for item in _children(drive, os.environ["DRIVE_APPROVED_SUMMARIES_FOLDER_ID"]):
        if not item["name"].endswith(".json"):
            continue
        content = drive.files().get_media(fileId=item["id"]).execute()
        summary = json.loads(content.decode("utf-8"))
        if summary.get("month") != first.strftime("%Y-%m") or summary.get("approved") is not True:
            continue
        takeaways.extend(summary.get("meeting_takeaways", []))
        guardrails.extend(summary.get("guardrail_activity", []))
        incidents.extend(summary.get("incidents_failures", []))
        sources.append({"kind": "approved_summary", "url": f"https://drive.google.com/file/d/{item['id']}/view",
                        "claim": "President-approved private meeting summary"})
    if not sources:
        raise ValueError("No verified merged changes; monthly report requires president brief")
    return {"title": "Monthly Lab report", "label": first.strftime("%Y-%m"), "month": first.strftime("%B %Y"),
            "shipped_projects": [item["title"] for item in merged], "active_projects": [],
            "meeting_takeaways": takeaways, "guardrail_activity": guardrails,
            "incidents_failures": incidents, "sources": sources}


def monthly_tick(now: datetime, drafts: Path, assets: Path) -> str:
    local = now.astimezone(NY)
    if local.hour != 12:
        return "no monthly action due"
    next_month = local.date().replace(day=28) + timedelta(days=4)
    final_day = next_month.replace(day=1) - timedelta(days=1)
    while final_day.weekday() >= 5:
        final_day -= timedelta(days=1)
    key = f"monthly-{local.year}-{local.month:02d}"
    if local.date() == final_day - timedelta(days=3) and not _job(key):
        brief = monthly_brief(local.year, local.month)
        brief["approval_deadline"] = datetime.combine(final_day, datetime.min.time(), NY).replace(hour=12).astimezone(timezone.utc).isoformat()
        campaign_id = _draft_and_route("monthly", brief, drafts, assets)
        _record_job(key, campaign_id)
        return f"monthly draft routed: {campaign_id}"
    if local.date() == final_day:
        campaign_id = _job(key)
        if not campaign_id:
            return "monthly report missing"
        row_number, row = _campaign_row(campaign_id)
        if row and row[3] == "awaiting_approval":
            _set_state(row_number, "skipped")
            return "monthly report skipped without approval"
        return "president publishes approved monthly report"
    return "no monthly action due"
