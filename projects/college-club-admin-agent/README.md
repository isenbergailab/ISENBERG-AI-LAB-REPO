---
name: College Club Admin Agent
slug: college-club-admin-agent
lead: isenbergailab
status: active
summary: Approval-gated club communications with private deployment state.
data_tier: synthetic
environment: cloud-workspace
sandbox:
  isolated: true
  disposable: true
  fake_inputs: true
  capped: true
  watched: true
approval_actions: [president approval before publication]
unattended_runs: true
officer_review: president review before deployment
spend_cap_usd: 5
spend_to_date_usd: 0
kill_switch: disable private runner workflow and Cloudflare Worker
logs: private Drive Operations admin-logs for ninety days
teardown: not-started
---

# College Club Admin Agent

**Lead:** Isenberg AI Lab
**Status:** In progress

Reusable, approval-gated club communications. This Isenberg-first deployment drafts meeting, merged-project, and monthly-report packages. The president approves each package. The agent never publishes public content automatically.

## Phases

1. Generate branded Instagram, LinkedIn, X, Slack, and Substack packages. Send one onboarding email from Lab Gmail. Track campaign states in a private Google Sheet.
2. Collect Monday topics in Slack. Route approvals through private Slack buttons. Draft project posts after merges. Produce native scheduling checklists.
3. Accept a Zoom cloud-recording event through a Cloudflare Worker. Import recordings into private Drive. Process manually exported My Notes transcripts. Enforce seven-day recording and ninety-day transcript limits.

## Quick start

Install Python 3.12 and `pip install -r requirements.txt`. Run:

```text
python -m college_club_admin draft --kind weekly --input examples/weekly.json --output ./drafts
python -m college_club_admin draft --kind merge --input examples/merge.json --output ./drafts
python -m college_club_admin draft --kind monthly --input examples/monthly.json --output ./drafts
python -m unittest discover -s tests
```

Set `PYTHONPATH=src` when running from this folder. Generated folders include the post copy, Instagram cards, X package, approval manifest, and sources. Drafting is deterministic by default. `--model` requests one short copy hook from Qwen3.5 Flash, with one retry. Failure skips the model hook.

## Deployment

Read [DEPLOYMENT.md](DEPLOYMENT.md) before connecting accounts. Keep deployment secrets, campaign data, transcripts, and recordings in a **private** deployment repository and private Drive. Never add them here. Public code is safe to share; example data is synthetic.

## Controls

- Only verified, structured claims enter public drafts. Transcripts create private summary proposals, never public quotations.
- Every public package requires president approval. Missed deadlines become `skipped`.
- X remains manual. Assisted native scheduling means the president schedules approved packages in each platform.
- No member names appear in public copy. Images containing identifiable people need separate media consent.
- Slack is the member communications channel. Email is only for interest-form onboarding.
- Model use is capped by its OpenRouter workspace key. No model fallback occurs.

## Tools

Python, Pillow, Google Apps Script, Cloudflare Workers, Slack, Zoom, Google Drive, GitHub Actions, OpenRouter.
