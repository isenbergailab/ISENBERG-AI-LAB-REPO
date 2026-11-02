---
name: automatic-anki-flashcard-creation
description: Create validated Anki TSV cards from messages.
version: 0.1.0
author: Drewc (drewc), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [anki, flashcards, language-learning, tsv, messaging]
    related_skills: []
---

# Automatic Anki Flashcard Creation

Turn eligible vocabulary screenshots or text messages into reviewed, four-field TSV batches for manual import into Anki Desktop. Hermes performs image/text interpretation; the bundled Portable Core owns configuration, identity isolation, duplicate handling, HTML escaping, atomic naming, state, export, and validation.

## When to Use

Use when a user:

- Sends a dictionary-entry screenshot and wants an Anki card.
- Sends one word, phrase, or an explicitly separable vocabulary list.
- Wants to collect cards into a batch and export a TSV.
- Wants the workflow through Hermes, Telegram, Discord, or a compatible adapter.

Do not use for arbitrary textbook-page segmentation, handwriting recognition, audio transcription, direct Anki database writes, or automatic AnkiWeb imports.

## Prerequisites

- Image-reading capability for screenshot inputs.
- Anki Desktop for manual import. The core workflow requires no Anki add-ons.
- A profile-local configuration created from `config/example.json`.

Read the matching setup guide before changing configuration:

- `references/anki-beginner-guide.md`
- `references/hermes-beginner-guide.md`
- `references/telegram-setup.md`
- `references/discord-setup.md`

## Runtime Paths

Resolve every relative file in this skill against the loaded skill directory. Store that absolute directory as `SKILL_DIR` conceptually; never assume the process working directory is the skill directory.

The Portable Core is:

```text
<SKILL_DIR>/scripts/automatic_anki_core.py
```

Configuration discovery order:

1. Explicit `--config` path
2. `AUTOMATIC_ANKI_CONFIG`
3. `$HERMES_HOME/automatic-anki-flashcard-creation/config.json`
4. `~/.hermes/automatic-anki-flashcard-creation/config.json`

All relative state/output paths resolve from the directory containing `config.json`. Built-in profile names resolve from `<SKILL_DIR>/language-profiles/`.

## Card Contract

Every card has exactly four ordered semantic roles:

1. **Prompt** — the stable item the learner recalls from.
2. **Answer** — the concise meaning or response.
3. **Notes** — reading, pronunciation, grammar, gender, or profile-specific information.
4. **Example** — one or more complete plain-text example lines.

Anki field names may differ. The configuration maps these roles to four distinct Anki fields. The Portable Core escapes all submitted markup and inserts only controlled `<br>` separators between Example lines.

## Procedure

### 1. Initialize and validate configuration

If no configuration exists, invoke the core by absolute path through `terminal`:

```text
python <SKILL_DIR>/scripts/automatic_anki_core.py init --hermes-home <ACTIVE_HERMES_HOME>
```

Then validate it:

```text
python <SKILL_DIR>/scripts/automatic_anki_core.py check-config --config <CONFIG_PATH>
```

Do not continue if the command fails. The core requires matching config/profile languages and four distinct field mappings.

Completion criterion: `check-config` returns JSON with `status: valid`.

### 2. Inventory the submitted material

Collect every attachment and explicit text item in the uninterrupted submission. Assign each a stable source identifier using provider message/attachment identifiers when available. A single dictionary image normally represents one candidate; an explicitly separable list may produce one `list-item` candidate per item.

Completion criterion: every candidate source is counted exactly once.

### 3. Decide eligibility before extracting fields

Eligible sources are:

- One dictionary entry representing one study item.
- One clearly identified word or phrase in text.
- One list whose items can be separated without interpretation.

Ambiguous images, arbitrary pages, handwriting, audio, or multiple inseparable entries are ineligible. Create a candidate JSON with `eligible: false`, `source_id`, `source_type`, and `rejection_reason`, then add it to the Batch so the rejection appears in the Validation Report.

Completion criterion: every source is either an eligible candidate or a recorded rejection.

### 4. Extract and apply the Language Profile

For screenshots, use image analysis to transcribe the headword, pronunciation or reading, definitions, usage labels, and supplied examples. For text, preserve the submitted item unless the profile defines a documented normalization.

Treat clear annotations as user intent. When one definition is distinctly circled or highlighted, prefer only that marked meaning unless the user says otherwise.

Populate the four roles according to the selected Language Profile. Do not apply Japanese-specific rules to other languages.

Completion criterion: each eligible source has four profile-compliant values and each value is classified as source, reference, or generated.

### 5. Enrich only missing content

The submitted source is authoritative. Consult approved references only when required content is absent or unclear. Generate an example only when the source and approved references do not provide one.

Mark uncertain roles and set `review_required` when a human should check the card. Do not put citations in the four fields; provenance belongs in the report.

Completion criterion: every non-source value has explicit provenance and every uncertainty is named.

### 6. Write a candidate JSON in scratch storage

Use `write_file` in the session scratch directory, not inside the skill or Git repository. Eligible candidate format:

```json
{
  "eligible": true,
  "source_id": "provider-message-attachment-id",
  "source_type": "image",
  "fields": {
    "Prompt": "食べる",
    "Answer": "to eat",
    "Notes": "Reading: たべる",
    "Example": [
      "毎朝、家族と朝ご飯を食べる。",
      "I eat breakfast with my family every morning."
    ]
  },
  "provenance": {
    "Prompt": "source",
    "Answer": "source",
    "Notes": "source",
    "Example": "reference"
  },
  "uncertain_fields": [],
  "review_required": false,
  "issues": []
}
```

Use plain text only. Never inject HTML. `Example` is a non-empty list; the core escapes each line and joins them with `<br>`.

Completion criterion: candidate JSON parses and contains no credentials or machine-local user data beyond its temporary source reference.

### 7. Add the candidate to the identity-scoped Batch

Invoke by absolute path:

```text
python <SKILL_DIR>/scripts/automatic_anki_core.py add --config <CONFIG_PATH> --adapter <ADAPTER> --user-id <USER_ID> --conversation-id <CONVERSATION_ID> --source <CANDIDATE_JSON>
```

The core hashes the stable adapter/user/conversation identity into a storage key. It applies the configured duplicate policy:

- `reject` — keep the first card and record the later duplicate.
- `merge` — fill missing values; retain first conflicting values and require review.
- `allow` — preserve both and record the duplicate.

Do not implement duplicate handling manually outside the core.

Completion criterion: the command returns `added`, `rejected`, `duplicate-rejected`, or `duplicate-merged` and an accurate Batch count.

### 8. Continue or finalize the Batch

Repeat steps 2–7 for new material. An explicit user export command is authoritative.

An adapter may request `--finalized-by inactivity` only when `batch.inactivity_seconds` is greater than zero and the configured interval has actually elapsed. The adapter owns the timer; the core enforces whether inactivity export is enabled.

Completion criterion: no candidate write is in progress when export begins.

### 9. Export atomically

Invoke:

```text
python <SKILL_DIR>/scripts/automatic_anki_core.py export --config <CONFIG_PATH> --adapter <ADAPTER> --user-id <USER_ID> --conversation-id <CONVERSATION_ID> --finalized-by explicit
```

The core:

- Selects the identity's next NATO name.
- Atomically reserves the TSV and report without overwrite.
- Writes exactly four escaped fields per row.
- Writes the complete Validation Report.
- Runs the validator with the finalized card count.
- Clears the active Batch only after validation succeeds.
- Returns absolute `tsv`, `report`, and `media` values.

Completion criterion: the command returns `status: exported` and a verified `card_count`.

### 10. Deliver through the originating adapter

Return both `media` lines from the export result through the same Message Adapter that received the source. In Hermes messaging surfaces they have this form:

```text
MEDIA:/absolute/host-readable/path/Alpha.tsv
MEDIA:/absolute/host-readable/path/Alpha.validation.json
```

Report the verified card count and whether any report item has `review_required: true`. Do not expose credentials, state paths, or unrelated local paths.

Completion criterion: the provider confirms both native file submissions or the user receives a clear delivery error.

## Message Adapter Contract

Every adapter must provide a stable user identity, conversation identity, source attachments or text, timestamps for optional inactivity finalization, and native file delivery. See `references/message-adapter-contract.md`.

An adapter transports sources and exports. The agent performs interpretation; the Portable Core and Language Profile own deterministic behavior.

## Security and Privacy

- Keep model keys and bot tokens outside the repository and chat history.
- Allowlist intended users or use pairing.
- Request minimum messaging permissions.
- Do not commit screenshots, candidates, generated exports, reports, state, or user configuration.
- Treat downloaded attachments as user data.
- Never manipulate `collection.anki2` or other Anki internals.
- Preserve HTML escaping; do not bypass the core by hand-writing user content into TSV.

## Pitfalls

- Anki imports TSV through Anki Desktop, then syncs to AnkiWeb.
- Anki checks the first field when matching imported notes; choose Prompt deliberately.
- Enable HTML during import so the core's controlled `<br>` separator renders correctly.
- Keep all four columns even when Notes is empty.
- Telegram usernames are not numeric allowed-user IDs.
- Never grant Discord Administrator permission.
- A host gateway can upload only host-readable paths.
- Do not promise an adapter is supported until native file delivery is tested.

## Verification

From any directory, use absolute skill paths or enter the package root for development checks:

1. Run `python -m unittest discover -s tests -v` in the package root.
2. Run `python scripts/validate_package.py` in the package root.
3. Validate `examples/Alpha.tsv` with the required expected count.
4. Run the Portable Core tests for image, text, rejection, duplicates, HTML escaping, identity isolation, naming, and delivery metadata.
5. Follow the beginner guide from an empty Anki profile.
6. Import the example TSV and verify field mapping and rendered `<br>`.
7. Test native attachment delivery through every adapter claimed as tested.
