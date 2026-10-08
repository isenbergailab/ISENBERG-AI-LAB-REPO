"""Private transcript screening. Human review remains mandatory."""

import re
from .campaigns import EMAIL, PHONE, HANDLE

SENSITIVE = re.compile(r"\b(?:ssn|social security|password|api key|secret key|medical|diagnos(?:is|ed)|home address)\b", re.I)


def review_transcript(text: str) -> dict:
    if len(text) > 1_000_000:
        raise ValueError("Transcript exceeds local review limit")
    findings = []
    if EMAIL.search(text):
        findings.append("email")
    if PHONE.search(text):
        findings.append("phone")
    if HANDLE.search(text):
        findings.append("handle")
    if SENSITIVE.search(text):
        findings.append("sensitive_topic")
    if findings:
        return {"status": "blocked_private_review", "findings": findings,
                "review_text": "", "approved_summary": "", "public_use": False}
    # No automatic quotations or public drafts. A reviewer writes any retained summary.
    return {"status": "awaiting_private_review", "findings": [],
            "review_text": "Review raw transcript privately. Write a deidentified summary yourself.",
            "approved_summary": "", "public_use": False}
