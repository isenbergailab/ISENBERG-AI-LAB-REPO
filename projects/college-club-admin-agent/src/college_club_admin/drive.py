"""Lab Gmail Drive access and explicit retention commands."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _api():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    token_path = os.environ["GOOGLE_OAUTH_TOKEN_JSON"]
    credentials = Credentials.from_authorized_user_file(token_path, ["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _children(api, folder_id: str):
    token = None
    while True:
        result = api.files().list(q=f"'{folder_id}' in parents and trashed = false", fields="nextPageToken,files(id,name,mimeType,createdTime,description)",
                                  pageSize=100, pageToken=token).execute()
        yield from result.get("files", [])
        token = result.get("nextPageToken")
        if not token:
            break


def enforce_retention(*, apply: bool = False) -> list[str]:
    api = _api()
    now = datetime.now(timezone.utc)
    rules = (("DRIVE_RECORDINGS_FOLDER_ID", 7), ("DRIVE_TRANSCRIPTS_FOLDER_ID", 90),
             ("DRIVE_TRANSCRIPTS_INBOX_ID", 90), ("DRIVE_LOGS_FOLDER_ID", 90))
    actions = []
    for env_name, days in rules:
        folder = os.environ[env_name]
        for item in _children(api, folder):
            created = datetime.fromisoformat(item["createdTime"].replace("Z", "+00:00"))
            if created > now - timedelta(days=days):
                continue
            actions.append(f"{'DELETED' if apply else 'WOULD_DELETE'} {env_name} {item['id']} {item['name']}")
            if apply:
                api.files().delete(fileId=item["id"]).execute()
    return actions


def upload_file(path: Path, folder_id: str, *, description: str = "") -> str:
    from googleapiclient.http import MediaFileUpload
    api = _api()
    media = MediaFileUpload(str(path), resumable=True)
    item = api.files().create(body={"name": path.name, "parents": [folder_id], "description": description},
                              media_body=media, fields="id").execute()
    return item["id"]


def upload_package(folder: Path) -> str:
    api = _api()
    parent = os.environ["DRIVE_CAMPAIGN_DRAFTS_FOLDER_ID"]
    created = api.files().create(body={"name": folder.name, "mimeType": "application/vnd.google-apps.folder",
                                        "parents": [parent]}, fields="id").execute()
    folder_id = created["id"]
    manifest = folder / "campaign.json"
    if manifest.exists():
        campaign = json.loads(manifest.read_text(encoding="utf-8"))
        campaign["private_package_url"] = f"https://drive.google.com/drive/folders/{folder_id}"
        manifest.write_text(json.dumps(campaign, indent=2, ensure_ascii=False), encoding="utf-8")
    for path in sorted(folder.iterdir()):
        if path.is_file():
            upload_file(path, folder_id)
    return f"https://drive.google.com/drive/folders/{folder_id}"


def scan_transcripts(output_folder: Path) -> list[Path]:
    """Download My Notes text exports into a private runner, then screen locally."""
    from googleapiclient.http import MediaIoBaseDownload
    from io import BytesIO
    from .transcripts import review_transcript
    api = _api()
    output_folder.mkdir(parents=True, exist_ok=True)
    results = []
    for item in _children(api, os.environ["DRIVE_TRANSCRIPTS_INBOX_ID"]):
        if not item["name"].lower().endswith(".txt"):
            continue
        if item.get("description", "").startswith("screened:"):
            continue
        destination = output_folder / f"{item['id']}-review.json"
        if destination.exists():
            continue
        stream = BytesIO()
        request = api.files().get_media(fileId=item["id"])
        downloader = MediaIoBaseDownload(stream, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        proposal = review_transcript(stream.getvalue().decode("utf-8"))
        destination.write_text(json.dumps(proposal, indent=2), encoding="utf-8")
        review_id = upload_file(destination, os.environ["DRIVE_LOGS_FOLDER_ID"], description="Private transcript screening status")
        api.files().update(fileId=item["id"], body={"description": f"screened:{proposal['status']}"}, fields="id").execute()
        from .slack import _post
        _post(os.environ["SLACK_APPROVAL_CHANNEL_ID"],
              f"My Notes transcript needs private review: {proposal['status']}. Review status: https://drive.google.com/file/d/{review_id}/view")
        results.append(destination)
    return results
