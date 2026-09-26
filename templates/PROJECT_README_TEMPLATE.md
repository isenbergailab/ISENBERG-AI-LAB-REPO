---
# Project header. Required before a project goes on the open project board.
# The monthly report script reads these fields. Keep values short.
name: Project Name
slug: project-name            # must match the folder name under projects/
lead: GitHub-username
contributors: []              # GitHub usernames
status: proposed              # proposed | active | shipped | paused | failed | archived
started: 2026-10-01
shipped:                      # date, or leave blank
summary: One sentence on what it does.

# How it was built
models: []                    # e.g. [openrouter/llama-3.3-70b, local/qwen2.5-7b]
stack: []                     # e.g. [python, docker, n8n]

# Guardrails (see Code of Conduct and Sandbox Guardrails)
data_tier: synthetic          # public | synthetic | personal | restricted
environment: docker           # docker | vm | cloud-workspace
sandbox:                      # all five must be true, or officer review is required
  isolated: true
  disposable: true
  fake_inputs: true
  capped: true
  watched: true
approval_actions: []          # agent actions that need a member to approve each time
unattended_runs: false        # true only after officer review
officer_review:               # officer username + date, if required
spend_cap_usd: 10
spend_to_date_usd: 0
kill_switch: How to stop it, in one line.
logs: Where prompts, tool calls, and outputs are saved.
teardown: not-started         # not-started | done | n/a
incidents: []                 # e.g. [{date: 2026-10-12, summary: "...", writeup: "docs/..."}]
---

# Project Name

One paragraph. What problem, who it helps, what it does today.

## Status

What works. What does not. Be specific.

## How to run it

1. Clone the repo and `cd projects/project-name`.
2. Copy `.env.example` to `.env` and add your own capped key.
3. `docker compose up`
4. To stop: (same as kill_switch above)

## How it was built

Models, tools, and why they were chosen. Credit anything you built on,
with license.

## Results

What was actually observed, with the task it was run on. Include failed runs.

## Negative results

What did not work and why. This section stays even after the project ships.
