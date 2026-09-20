# jev-browser

Drive a real browser with [TypeSafe](https://typesafe.ai/)'s Jev — a System One
model that returns typed judgments. Give it a URL and a task: a visible Chromium
window opens, and Jev decides every click, form fill, and navigation step from
the page's live interactive elements. Code owns the loop; Jev supplies the
per-step semantic decision.

## How it works

Each step the script:

1. Snapshots the page's visible interactive elements (links, buttons, inputs)
   into a numbered list with role, name, value, and href.
2. Sends **one** Jev request with that state plus the task, the URL, the page
   title, and the actions taken so far, asking three questions:
   - `next_action` — **Choice** over element ids plus `done` / `stuck`
   - `task_complete` — **Noul** (is the task finished on this page?)
   - `risky` — **Noul** (would the action purchase, delete, or submit personal data?)
3. Acts on the answer (click, fill, check), re-snapshots, and repeats.

## Prerequisites

- Python 3.10+
- `pip install typesafe-sdk playwright` then `playwright install chromium`
- A `TYPESAFE_API_KEY` from https://console.typesafe.ai/

## Install

```powershell
git clone https://github.com/isenbergailab/ISENBERG-AI-LAB-REPO.git
cd ISENBERG-AI-LAB-REPO/skills/jev-browser
./install.ps1
```

The installer copies the skill to `~/.agents/skills/jev-browser` (where agent
tools such as [opencode](https://opencode.ai) pick it up) and registers a `jev`
PowerShell function for manual use. Reload your profile or open a new terminal
afterwards.

## Usage

```powershell
jev -Url "https://example.com" -Task "Search for ___ and open the first result" -Steps 25
```

or, directly:

```powershell
py -3 browse.py --url "https://example.com" --task "Start a new game with 5 minutes per side and a 3 second increment" --steps 25
```

| Flag | Meaning |
| --- | --- |
| `--url` | page to open (required) |
| `--task` | natural-language task for Jev (required) |
| `--steps` | max actions before stopping (default 25) |
| `--headless` | run without a visible browser window |
| `--guidance` | user guidance collected during a previous paused run |

## Safety behavior

The script deliberately pauses instead of guessing:

- **Low confidence** (Choice confidence < 0.3) or `stuck` — prints the visible
  elements and asks for guidance.
- **Risky action** (risky probability > 0.5) — prints the element and requires
  a typed `yes` before purchasing, deleting, or submitting personal data.
- **Unfillable fields** — text inputs whose value cannot be derived from the
  task (passwords, emails, payment details) are left empty, listed in
  `unfilled_fields`, and the run pauses for you to type them.

Credentials are read from the `TYPESAFE_API_KEY` environment variable only and
are never logged or stored.

## Statuses

`success` | `paused` | `stuck` | `limit` | `aborted` | `error`

## License

MIT — see [LICENSE](LICENSE).
