"""Local review ledger and optional private Google Sheet mirror."""

import json
from datetime import datetime, timezone
from pathlib import Path

TRANSITIONS = {
    "draft": {"awaiting_approval", "skipped"},
    "awaiting_approval": {"approved", "skipped"},
    "approved": {"scheduled", "published", "skipped"},
    "scheduled": {"published", "skipped"},
    "published": {"archived"},
    "skipped": {"archived"},
    "archived": set(),
}
HEADERS = ["id", "kind", "title", "state", "created_on", "updated_at", "approved_by", "approved_at", "deadline", "package_path", "instagram_url", "linkedin_url", "x_url", "substack_url", "slack_url"]


def transition(campaign: dict, state: str, *, actor: str = "", permalink: str = "") -> dict:
    current = campaign["state"]
    if state not in TRANSITIONS.get(current, set()):
        raise ValueError(f"Invalid transition: {current} -> {state}")
    deadline = campaign["approval"].get("deadline", "")
    now = datetime.now(timezone.utc)
    if state == "approved":
        if not actor:
            raise ValueError("Approval requires president identity")
        if deadline and now > datetime.fromisoformat(deadline.replace("Z", "+00:00")):
            raise ValueError("Approval deadline passed; skip campaign")
        campaign["approval"].update(approved_by=actor, approved_at=now.isoformat())
    if state in {"scheduled", "published"} and not campaign["approval"].get("approved_by"):
        raise ValueError("Unapproved campaign cannot publish")
    if permalink:
        if not permalink.startswith("https://"):
            raise ValueError("Permalink must use HTTPS")
        campaign["publication"]["x"] = permalink
    campaign["state"] = state
    campaign["updated_at"] = now.isoformat()
    return campaign


def sync_sheet(campaign: dict, package_path: Path) -> None:
    """Update a private sheet when service account configuration exists."""
    import os
    sheet_id = os.environ.get("CAMPAIGN_SHEET_ID")
    credentials_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sheet_id or not credentials_path:
        return
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    credentials = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    api = build("sheets", "v4", credentials=credentials, cache_discovery=False).spreadsheets().values()
    table = api.get(spreadsheetId=sheet_id, range="Campaigns!A:O").execute().get("values", [])
    if table and table[0] != HEADERS:
        raise ValueError("Campaigns sheet headers differ from expected schema")
    if not table:
        api.update(spreadsheetId=sheet_id, range="Campaigns!A1:O1", valueInputOption="RAW", body={"values": [HEADERS]}).execute()
    row = [campaign["id"], campaign["kind"], campaign["title"], campaign["state"], campaign["created_on"],
           campaign.get("updated_at", ""), campaign["approval"].get("approved_by", ""),
           campaign["approval"].get("approved_at", ""), campaign["approval"].get("deadline", ""),
           campaign.get("private_package_url", str(package_path)), *(campaign["publication"].get(channel, "") for channel in ("instagram", "linkedin", "x", "substack", "slack"))]
    for index, existing in enumerate(table[1:], start=2):
        if existing and existing[0] == campaign["id"]:
            api.update(spreadsheetId=sheet_id, range=f"Campaigns!A{index}:O{index}", valueInputOption="RAW", body={"values": [row]}).execute()
            return
    api.append(spreadsheetId=sheet_id, range="Campaigns!A:O", valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": [row]}).execute()


def save_campaign(folder: Path, campaign: dict) -> None:
    (folder / "campaign.json").write_text(json.dumps(campaign, indent=2, ensure_ascii=False), encoding="utf-8")
    sync_sheet(campaign, folder)
