"""Meeting capture requires a president-certified consent row."""

import os
from google.oauth2 import service_account
from googleapiclient.discovery import build


def verify_meeting_consent(meeting_uuid: str) -> bool:
    credentials = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    values = build("sheets", "v4", credentials=credentials, cache_discovery=False).spreadsheets().values()
    rows = values.get(spreadsheetId=os.environ["CAMPAIGN_SHEET_ID"], range="Consent!A:D").execute().get("values", [])
    for row in rows[1:]:
        if len(row) >= 4 and row[0] == meeting_uuid:
            return row[1] == "YES" and row[2] == os.environ["PRESIDENT_SLACK_ID"] and bool(row[3])
    return False
