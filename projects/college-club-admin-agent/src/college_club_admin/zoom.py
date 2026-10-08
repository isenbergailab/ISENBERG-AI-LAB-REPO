"""Import authorized Zoom cloud recordings directly into private Drive."""

import base64
import os
import tempfile
from pathlib import Path
from urllib.parse import quote
import requests
from datetime import datetime, timedelta, timezone

from .drive import upload_file, _api, _children


def _token() -> str:
    basic = base64.b64encode(f"{os.environ['ZOOM_CLIENT_ID']}:{os.environ['ZOOM_CLIENT_SECRET']}".encode()).decode()
    response = requests.post("https://zoom.us/oauth/token", params={"grant_type": "account_credentials", "account_id": os.environ["ZOOM_ACCOUNT_ID"]},
                             headers={"Authorization": f"Basic {basic}"}, timeout=20)
    response.raise_for_status()
    return response.json()["access_token"]


def import_recording(meeting_uuid: str) -> list[str]:
    if not meeting_uuid or len(meeting_uuid) > 200:
        raise ValueError("Invalid Zoom meeting UUID")
    from .consent import verify_meeting_consent
    if not verify_meeting_consent(meeting_uuid):
        raise PermissionError("President-certified meeting consent is missing")
    token = _token()
    endpoint = f"https://api.zoom.us/v2/meetings/{quote(meeting_uuid, safe='')}/recordings"
    result = requests.get(endpoint, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    result.raise_for_status()
    meeting = result.json()
    if meeting.get("host_id") != os.environ["ZOOM_AUTHORIZED_HOST_ID"]:
        raise PermissionError("Recording host is not authorized")
    stored = []
    for recording in meeting.get("recording_files", []):
        if recording.get("file_type") != "MP4" or recording.get("status") != "completed":
            continue
        file_id = recording["id"]
        name = f"zoom-{meeting_uuid.replace('/', '_')[:48]}-{file_id}.mp4"
        with tempfile.TemporaryDirectory(prefix="lab-zoom-") as temporary:
            path = Path(temporary) / name
            with requests.get(recording["download_url"], headers={"Authorization": f"Bearer {token}"},
                              stream=True, timeout=120) as download:
                download.raise_for_status()
                with path.open("wb") as target:
                    for chunk in download.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            target.write(chunk)
            stored.append(upload_file(path, os.environ["DRIVE_RECORDINGS_FOLDER_ID"], description=f"Zoom recording {file_id}"))
    return stored


def purge_old_cloud_recordings() -> list[str]:
    """Delete only authorized recordings already copied into private Drive."""
    token = _token()
    now = datetime.now(timezone.utc)
    copied = {item.get("description", "").removeprefix("Zoom recording ")
              for item in _children(_api(), os.environ["DRIVE_RECORDINGS_FOLDER_ID"])}
    results = []
    page_token = ""
    while True:
        params = {"from": (now - timedelta(days=30)).date().isoformat(),
                  "to": now.date().isoformat(), "page_size": 100}
        if page_token:
            params["next_page_token"] = page_token
        listing = requests.get(f"https://api.zoom.us/v2/users/{quote(os.environ['ZOOM_AUTHORIZED_HOST_ID'], safe='')}/recordings",
                               headers={"Authorization": f"Bearer {token}"}, params=params, timeout=20)
        listing.raise_for_status()
        data = listing.json()
        for meeting in data.get("meetings", []):
            started = datetime.fromisoformat(meeting["start_time"].replace("Z", "+00:00"))
            if started > now - timedelta(days=7) or meeting.get("host_id") != os.environ["ZOOM_AUTHORIZED_HOST_ID"]:
                continue
            files = [file for file in meeting.get("recording_files", []) if file.get("file_type") == "MP4"]
            if not files or not all(file.get("id") in copied for file in files):
                results.append(f"SKIPPED_UNCOPIED {meeting.get('uuid', '')}")
                continue
            uuid = quote(meeting["uuid"], safe="")
            response = requests.delete(f"https://api.zoom.us/v2/meetings/{uuid}/recordings",
                                       headers={"Authorization": f"Bearer {token}"}, timeout=20)
            response.raise_for_status()
            results.append(f"ZOOM_DELETE_REQUESTED {meeting['uuid']}")
        page_token = data.get("next_page_token", "")
        if not page_token:
            break
    return results
