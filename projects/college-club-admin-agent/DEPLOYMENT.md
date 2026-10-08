# Isenberg deployment checklist

Code is implemented locally. Live integrations require lab-owned accounts and credentials. Complete setup from `isenbergailab@gmail.com` and the authorized UMass Zoom account. Keep all credentials in the private deployment repository and Cloudflare secrets.

## 1. Private storage

Create these folders in the Lab Google Drive. Give access only to the president and current operators.

```text
Marketing/
  campaign-drafts/
  published-packages/
  brand-assets/
Operations/
  recordings/
  transcripts/
    inbox/
  approved-summaries/
  onboarding-records/
  approvals/
  admin-logs/
```

Create a private Google Sheet with tabs `Campaigns`, `Jobs`, and `Consent`. Share the Sheet with a narrowly scoped service account. The campaign header row is defined in `src/college_club_admin/ledger.py`. The `Jobs` tab has three columns: key, value, timestamp. The `Consent` tab has four columns: meeting UUID, consent complete (`YES`), certifying president Slack ID, and certification timestamp. Create a Google Cloud OAuth desktop client under the Lab account, enable Drive API, then run `python deployment/oauth_setup.py --client-secrets <private-client-json> --output <private-token-json>` while signed into Lab Gmail. Store the authorized-user token JSON only in private deployment secrets. Drive access requires the full Drive scope because members manually place My Notes exports in the inbox.

## 2. OpenRouter

Create the OpenRouter organization from Lab Gmail. Add each president through their own account. Create workspace `social-agent`, then a workspace-owned key. Allow only `qwen/qwen3.5-flash-02-23`. Apply a $1 monthly key cap, ZDR, and training-disabled routing. Rotate the key annually during the four-week handoff. The private runner uses `OPENROUTER_API_KEY`. Model requests use strict JSON. One retry occurs; a second failure skips that campaign.

## 3. Google onboarding

Open the existing interest response Sheet as Lab Gmail. Paste `apps-script/Code.gs` into its bound Apps Script project. Set Script Properties:

```text
EMAIL_COLUMN_NAME = Email Address
SLACK_JOIN_URL = current Lab Slack invite
GUIDE_URL = current new-member guide URL
REPO_URL = https://github.com/isenbergailab/ISENBERG-AI-LAB-REPO
GUARDRAILS_URL = current Lab guardrails page
WORKER_SHARED_SECRET = random long secret
PRESIDENT_SLACK_ID = current president user ID
SLACK_APPROVAL_CHANNEL_ID = private approval channel ID
CAMPAIGN_SHEET_ID = private campaign Sheet ID
```

Create an **installable** Sheets form-submit trigger for `onInterestFormSubmit` using Lab Gmail. Confirm a synthetic response receives exactly one welcome email. Check the status columns. The subject is `Welcome to Isenberg AI Lab`; the first line directs members to Slack. The script tries delivery twice, then marks the row for presidential review. Gmail delivery cannot guarantee exactly-once behavior after an ambiguous send error; review `FAILED_REVIEW` rows before resending.

Deploy Apps Script as a web app executing as Lab Gmail. Use its URL only in the Cloudflare Worker secret `APPS_SCRIPT_URL`. Limit web-app access as tightly as the campus setup permits. The body secret and president ID are rechecked before campaign state changes.

## 4. Slack and Cloudflare

Create a Slack app in the Lab workspace. Give it `chat:write` and `channels:history` or the equivalent scope for the member channel. Invite it to the member and private approval channels. Enable interactivity at `https://<worker>/slack/actions`. Install it and capture its bot token and signing secret.

Deploy `worker/` with Cloudflare Wrangler. Set Worker secrets: `SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN`, `SLACK_APPROVAL_CHANNEL_ID`, `PRESIDENT_SLACK_ID`, `WORKER_SHARED_SECRET`, `APPS_SCRIPT_URL`, `ZOOM_WEBHOOK_SECRET`, `ZOOM_ACCOUNT_ID`, `ZOOM_AUTHORIZED_HOST_ID`, `GITHUB_DISPATCH_TOKEN`, `PRIVATE_GH_OWNER`, `PRIVATE_GH_REPO`. Verify `/health`, then test a synthetic Slack decision against a private test campaign. The Worker verifies signatures and timestamps before forwarding decisions.

## 5. Private runner

Create private repository `isenbergailab/college-club-admin-private`. Copy `deployment/private-workflow.yml` into its `.github/workflows/` folder. Add every secret named in that workflow. Set both Google JSON secrets as base64 text. The runner checks New York local time, so daylight saving changes are handled by code. It posts Monday topic prompts, drafts a weekly campaign after noon, skips missing approvals, and sends Tuesday Slack reminders only for approved campaigns. It prepares monthly reports three days before the last business day when merged evidence or approved summaries exist. President reviews and publishes Substack, Instagram, LinkedIn, and X content natively. Private runner failures alert both the approval channel and Lab Gmail.

Copy `deployment/public-merge-workflow.yml` into the public repo `.github/workflows/` folder after the private repository exists. Set `PRIVATE_DEPLOYMENT_TOKEN` there with permission to dispatch only to the private repository. The public workflow sends merged PR metadata; private content never enters public Actions logs.

## 6. Zoom and My Notes

Use the authorized UMass host. Obtain campus approval for a Zoom app with cloud recording read and delete scopes. Enable the `recording.completed` webhook at `https://<worker>/zoom/events`. Cloudflare verifies Zoom signatures and dispatches only the meeting UUID to the private runner. The runner downloads completed MP4 files into `Operations/recordings/` after consent certification. If certification arrives after the webhook, rerun the private workflow with its `meeting_uuid` input. It deletes authorized Zoom recordings after seven days only when matching Drive copies exist; it permanently deletes Drive recordings after seven days.

At each meeting, announce recording and My Notes transcription. Obtain explicit consent from everyone, including late arrivals. Stop both if consent fails. Record the entire meeting. The president certifies consent in the private `Consent` sheet before recording import. Afterward, the president completes My Notes, copies its full transcript into a UTF-8 `.txt` file, and uploads that file into `Operations/transcripts/inbox/`. The private runner screens it locally. Any detected contact details or sensitive terms block drafting and require private review. No transcript generates automatic public quotations. A president-authored deidentified summary may be retained in `approved-summaries/` and used as a verified source. Use `examples/approved-summary.json` as the sanitized schema; replace every synthetic sentence with reviewed claims. Wednesday recaps require this file.

The retention job permanently deletes private Drive recordings after seven days, transcript files after ninety days, and operational logs after ninety days. Configure Zoom My Notes transcript retention to ninety days if UMass permits. Verify whether the UMass Zoom account retains deleted cloud recordings in Trash; campus policy can extend actual server retention beyond the agent's deletion request.

## 7. Succession

Four weeks before transition, add the incoming president to OpenRouter, Cloudflare, private GitHub repository, Slack app, Google Drive, Sheet, Apps Script, and Zoom as permitted. Transfer payment method. Rotate keys. Test the onboarding email, approval button, draft package, Zoom webhook, My Notes handoff, and retention job. Remove outgoing access when the new president decides. Keep private account inventory and recovery steps under `Operations/`, never the public repo.

## Cost guardrail

OpenRouter key cap is $1 monthly. Use free allowances for Apps Script, Cloudflare Worker, and GitHub Actions only after checking actual account eligibility and usage. Pause scheduled jobs if any platform reports charges that threaten the total $5 monthly cap. Native social scheduling and manual X posting avoid paid social APIs.
