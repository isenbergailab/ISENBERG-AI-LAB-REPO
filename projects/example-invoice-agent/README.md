---
name: Example Invoice Agent
slug: example-invoice-agent
lead: Tdepth
contributors: []
status: active
started: 2026-10-01
shipped:
summary: Sample project showing a filled-in header. Delete once real projects exist.
models: [openrouter/llama-3.3-70b]
stack: [python, docker]
data_tier: synthetic
environment: docker
sandbox:
  isolated: true
  disposable: true
  fake_inputs: true
  capped: true
  watched: false
approval_actions: [writing outside project folder]
unattended_runs: false
officer_review:
spend_cap_usd: 10
spend_to_date_usd: 1.40
kill_switch: docker compose down
logs: logs/ folder inside the container volume
teardown: not-started
incidents: []
---

# Example Invoice Agent

Reads fake invoices and flags duplicates. Sample only.

## Negative results

First version missed duplicates with different date formats.


