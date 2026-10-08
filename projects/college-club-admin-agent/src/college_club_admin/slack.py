"""Slack outgoing messages. Tokens live only in private deployment state."""

import os
import requests


def _post(channel: str, text: str, *, blocks=None):
    token = os.environ["SLACK_BOT_TOKEN"]
    body = {"channel": channel, "text": text}
    if blocks:
        body["blocks"] = blocks
    response = requests.post("https://slack.com/api/chat.postMessage",
                             headers={"Authorization": f"Bearer {token}"}, json=body, timeout=20)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "Slack post failed"))
    return result


def monday_thread(meeting_date: str):
    text = (f"Tuesday Lab meeting: {meeting_date}, 5:15–6 PM. Reply by Monday noon. "
            "What might you demonstrate? What AI news matters? What failed this week? "
            "What should members discuss? Demos and projects remain optional.")
    return _post(os.environ["SLACK_MEMBER_CHANNEL_ID"], text)


def request_approval(campaign: dict):
    link = campaign.get("private_package_url", "")
    if not link.startswith("https://drive.google.com/drive/folders/"):
        raise ValueError("Upload private package before requesting approval")
    text = f"Campaign {campaign['id']} awaits president approval. Review private package: {link}"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Approve"},
             "style": "primary", "action_id": "campaign_approve", "value": campaign["id"]},
            {"type": "button", "text": {"type": "plain_text", "text": "Skip"},
             "style": "danger", "action_id": "campaign_skip", "value": campaign["id"]},
        ]},
    ]
    return _post(os.environ["SLACK_APPROVAL_CHANNEL_ID"], text, blocks=blocks)
