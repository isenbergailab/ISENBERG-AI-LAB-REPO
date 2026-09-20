---
name: jev-browser
description: >
  Automate a website with Jev: give a URL and a task, and a real browser
  opens; Jev decides every click, form fill, and navigation step from the
  page's accessibility state. Pauses for the user when uncertain, when an
  action looks risky (purchase, delete, personal data), and for fields it
  cannot confidently fill. WHEN: "open a browser and do X", "automate this
  website", "browse to X and complete Y", "have Jev use this site",
  "browser automation with Jev", "click through this site and do a task",
  "go do X on this site for me", "sign me up on ...", "complete this form
  on ...", "use the browser to ...".
---

# Jev-Browser

Drive a real browser with TypeSafe's Jev. Code (the loop script) owns the
workflow; Jev makes the per-step semantic decision of which on-screen element
best advances the task. See the [TypeSafe docs](https://docs.typesafe.ai/llms.txt)
for the underlying primitives.

## Prerequisites

Check before running; install anything missing:

1. Python 3.10+
2. `pip install typesafe-sdk playwright` then `playwright install chromium`
3. `TYPESAFE_API_KEY` environment variable (from https://console.typesafe.ai/)

Note: on some Windows setups `python` is not on PATH; if the plain command
fails, invoke with `py -3`.

## Agent invocation

When the user describes a browsing task conversationally ("go do X on this
site for me"), resolve this skill's directory, then run the script yourself:

```powershell
py -3 "<skill-dir>/scripts/browse.py" --url "https://example.com" --task "Search for ___ and open the first result" --steps 25
```

Here `<skill-dir>` is this skill's install location, typically
`~/.agents/skills/jev-browser` — resolve it to the full absolute path at
runtime; never ask the user to type the command.

Never ask the user to run terminal commands. Run the script, watch its
output, and relay pauses (guidance prompts, risky-action confirmations,
unfillable fields) and the final result back conversationally. If the run
pauses for guidance, collect the user's answer and re-run with
`--guidance "<their answer>"`.

## Usage

- `--url` — page to open (required)
- `--task` — natural-language task for Jev (required)
- `--steps` — max actions before stopping (default 25)
- `--headless` — run without a visible browser window (default is headed so the user can watch)
- `--guidance` — user guidance collected during a previous paused run

Manual one-word invocation is also available via the `jev` PowerShell
function (see `jev.ps1` in this skill folder).

## How it works

Each step the script:

1. Snapshots the page's visible interactive elements (links, buttons, inputs)
   into a numbered list with role, name, value, and href.
2. Sends one Jev request with that state and three questions:
   - `next_action` — **Choice** over element ids plus `done` and `stuck`
   - `task_complete` — **Noul**, is the task done based on the current page
   - `risky` — **Noul**, would the chosen action purchase, delete, or submit
     personal data
3. Acts on the answer (click, type, select), then re-snapshots.

## Safety behavior (expect these pauses)

- **Low confidence** (Choice confidence < 0.3) or `stuck`: the script pauses
  and asks the user for guidance on the console. Relay the printed page
  summary to the user and pass their answer with `--guidance` on a re-run, or
  have the user answer in the paused terminal session.
- **Risky action** (risky probability > 0.5): the script prints the element
  and what Jev thinks it does and requires a typed `yes` before acting.
  Never bypass this.
- **Unfillable fields**: text inputs Jev cannot confidently fill (no clear
  value derivable from the task — e.g., passwords, payment details, arbitrary
  personal data) are left empty, listed in `unfilled_fields` in the output,
  and the run pauses for the user to type them directly in the browser or
  abort. Do not ask Jev to guess these.

## Interpreting output

The script prints a per-step log (step number, URL, chosen element, action,
confidence) and a final status line:

- `success` — task_complete confirmed or Jev chose `done`
- `paused` — waiting on the user (uncertainty, risk, or unfillable field)
- `stuck` — Jev found no element that advances the task; report the URL and
  task and suggest the user do this part manually
- `limit` — step cap reached; suggest re-running with more steps or a
  narrower task
- `aborted` — user declined a risky action

Relay the final summary and any `unfilled_fields` to the user verbatim.

## Troubleshooting

- `TYPESAFE_API_KEY` errors: point the user to https://console.typesafe.ai/
- SDK errors (rate limit, connection): retry once, then surface the message.
- Navigation timeouts: the script snapshots whatever loaded and lets Jev
  judge; if the page is genuinely broken, expect a `stuck` status.
- If answers look wrong, inspect the exact state and questions printed in the
  step log before changing thresholds.
