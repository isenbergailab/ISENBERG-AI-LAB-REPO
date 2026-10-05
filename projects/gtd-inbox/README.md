---
name: GTD Inbox Agent
slug: gtd-inbox
lead: unassigned
contributors: []
status: proposed
started: 2026-10-05
shipped:
summary: Approval-first GTD proposals for a synthetic Obsidian vault.
models: [typesafe/jev-1.13, z-ai/glm-5.3-flash, z-ai/glm-5.3]
stack: [python, docker]
data_tier: synthetic
environment: docker
sandbox:
  isolated: true
  disposable: true
  fake_inputs: true
  capped: true
  watched: true
approval_actions: [applying judgment proposals, enabling remote inference, connecting external accounts, exporting notes]
unattended_runs: false
officer_review:
spend_cap_usd: 0
spend_to_date_usd: 0
kill_switch: Press Ctrl+C; run docker compose down.
logs: Temporary sandbox state/logs and vault/_agent; no submitted runtime logs.
teardown: not-started
incidents: []
---

# GTD Inbox Agent

Local GTD captures become reviewable proposals. Markdown notes remain authoritative.
Users approve judgment before changes apply. Hash checks prevent stale writes.
Backups and Undo support recovery.

## Status

Proposed lab submission; maintainer remains unassigned. Publication license remains undecided.
This submission contains source and synthetic fixtures. Original runtime data and Git history are excluded.

Header describes supplied Docker demo configuration. It does not certify general production use.
See [validation](docs/VALIDATION.md) for actual checks and limitations.

## Run isolated demo

From repository root:

```sh
cd projects/gtd-inbox
docker compose build
docker compose run --rm demo
docker compose down
```

Demo creates disposable synthetic vault. One sample capture becomes a proposal.
Demo verifies destination stays unchanged before approval. Then explicit demo approval applies it.
Container has no network or host mounts. Root filesystem is read-only.
State and vault use temporary storage. No keys or paid calls occur.
Watch output while demo runs. Ctrl+C stops execution.

For development inside disposable VM:

```sh
python -m pip install -r requirements-dev.txt
python -B -m unittest discover -s tests
python -B tools/demo.py
python tools/check_public_files.py
```

Tests create temporary fake vaults. Mail tests use loopback fake IMAP.
TLS certificates are generated temporarily. No private keys ship here.

## Capabilities

- Inbox routing and structured extraction.
- Approval previews, feedback, and Undo.
- Follow-ups, digests, and weekly reviews.
- Markdown and optional PDF intake.
- Optional mail, calendar, Telegram integrations.

Demo disables integrations and remote inference. Their code remains for review.
They need separate approval and configuration. Never use real student or employer data here.

## Models and dependencies

Jev selects GTD category. GLM extracts structured fields.
Python validates fields and dates. Python enforces approvals and writes.

Models are optional external services. Configured model identifiers are inherited defaults.
Availability and paid contracts remain unverified. No model weights are included.
Python core uses standard library. Test dependencies include cryptography and pypdf.
See [credits](docs/CREDITS.md) and [domain terms](docs/DOMAIN.md).

## Guardrails

Use synthetic inputs in disposable sandbox. Remote inference defaults off.
Private markers block modeled content. These filters cannot guarantee complete detection.
Some bookkeeping can write automatically. Dry-run mode suppresses ordinary applied proposals;
generated support notes may still change. Always isolate entire writable vault.
No unattended runs are approved.

Runtime state can contain sensitive text. Ignore rules are supplementary protections.
Never publish live vaults or state. Run publication checks before every contribution.
See [privacy](docs/PRIVACY.md).

## Results

See [validation record](docs/VALIDATION.md). No private-data calls were performed.
No live account integration was tested.

## Negative results

Container execution needs working Docker access. Validation host initially lacked that access.
Live model availability remains unverified. Sync conflicts need independent testing.
Mail screening can miss sensitive material. Private Markdown relies on supplied markers.
Zero budget setting alone prevents no calls: supplied demo also disables remote inference
and removes networking. Paid inference requires provider-side spending limits.
