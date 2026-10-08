"""Entry point for private deployment repository workflows."""

import json
import os
import sys
import requests
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from college_club_admin.operations import weekly_tick, monthly_tick, merged_event
from college_club_admin.zoom import import_recording, purge_old_cloud_recordings
from college_club_admin.drive import enforce_retention, scan_transcripts


def main():
    event = sys.argv[1]
    drafts = Path(os.environ.get("PRIVATE_DRAFT_DIR", "./private-drafts"))
    assets = ROOT / "assets"
    if event == "tick":
        now = datetime.now(timezone.utc)
        print(weekly_tick(now, drafts, assets))
        print(monthly_tick(now, drafts, assets))
        if now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).hour == 9:
            print('\n'.join(purge_old_cloud_recordings()))
            print('\n'.join(enforce_retention(apply=True)))
            print('\n'.join(str(path) for path in scan_transcripts(drafts / 'transcript-reviews')))
    elif event == "merge":
        payload = json.loads(os.environ["MERGE_PAYLOAD_JSON"])
        print(merged_event(payload, drafts, assets))
    elif event == "zoom":
        print('\n'.join(import_recording(os.environ["ZOOM_MEETING_UUID"])))
    else:
        raise ValueError("Unknown deployment event")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        message = f"Private Lab job failed: {sys.argv[1] if len(sys.argv) > 1 else 'unknown'} ({type(error).__name__}). Review private runner logs."
        try:
            requests.post("https://slack.com/api/chat.postMessage",
                          headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
                          json={"channel": os.environ["SLACK_APPROVAL_CHANNEL_ID"], "text": message}, timeout=10)
            requests.post(os.environ["APPS_SCRIPT_URL"], json={"action": "alert", "secret": os.environ["WORKER_SHARED_SECRET"],
                                                            "message": message}, timeout=10)
        except Exception:
            pass
        raise
