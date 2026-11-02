---
name: Automatic Anki Flashcard Creation
slug: automatic-anki-flashcard-creation
lead: unassigned
status: proposed
summary: Reviewed vocabulary exports with synthetic offline validation.
data_tier: synthetic
environment: docker
sandbox:
  isolated: true
  disposable: true
  fake_inputs: true
  capped: true
  watched: true
approval_actions: [enabling remote interpretation, connecting messaging accounts, delivering attachments, importing cards into Anki]
unattended_runs: false
spend_cap_usd: 0
spend_to_date_usd: 0
kill_switch: Stop attended command with Ctrl+C; docker compose down.
logs: Test output only; runtime state and exports stay outside repository.
teardown: n/a
---

# Automatic Anki Flashcard Creation

A portable AI-agent skill that turns dictionary screenshots and vocabulary messages into validated four-field TSV batches for manual import into Anki Desktop.

## What it produces

Each card contains four ordered roles:

1. Prompt
2. Answer
3. Notes
4. Example

Japanese, Spanish, and Mandarin Language Profiles are included. Roles can map to any four distinct fields in an Anki note type; a template renderer generates matching Anki templates for custom field names.

Each adapter/user/conversation identity receives its own NATO sequence:

```text
Alpha.tsv ... Zulu.tsv
Alpha-2.tsv ... Zulu-2.tsv
```

Each TSV has a companion Validation Report identifying source provenance, generated or uncertain values, duplicate outcomes, and rejected sources.

## Included implementation

- Hermes skill procedure for image/text interpretation and native attachment delivery
- Deterministic Portable Core for configuration, batching, identity isolation, duplicate policies, HTML escaping, atomic filenames, export, and validation
- Telegram and Discord setup through the Hermes gateway
- Beginner Anki Desktop, template, import, sync, and optional-add-on guidance
- Standard-library tests and text/image/ambiguous/malformed/duplicate/security fixtures

The core TSV workflow requires no Anki add-ons and never writes directly to Anki's database.

## Start here

If you have never used Anki or an AI agent:

1. Follow [`references/anki-beginner-guide.md`](references/anki-beginner-guide.md).
2. Follow [`references/hermes-beginner-guide.md`](references/hermes-beginner-guide.md).
3. Install this complete directory as a Hermes skill.
4. Initialize a profile-local configuration with `scripts/automatic_anki_core.py init` as described in the Hermes guide.
5. Send one safe test word or dictionary screenshot.
6. Ask the agent to export the batch.
7. Review the Validation Report and import the TSV through Anki Desktop.

For Telegram or Discord, finish normal Hermes setup first, then follow the matching adapter guide. Provider delivery should be tested in a private conversation or test server before wider use.

## Repository map

```text
SKILL.md                         Agent procedure and Hermes reference path
DESIGN.md                        Accepted design and release criteria
GLOSSARY.md                      Canonical terminology
config/                          Configuration schema and example
language-profiles/               Language-specific rules and template
fixtures/sources/                Behavioral and security fixtures
templates/anki/                  Default four-field Anki templates
references/                      Beginner and contributor guides
scripts/automatic_anki_core.py   Deterministic batching/export core
scripts/render_anki_templates.py Custom-field template renderer
scripts/validate_export.py       TSV/report validator
scripts/validate_package.py      Package structural checks
tests/                           Standard-library automated tests
examples/                        Valid example export and report
docs/adr/                        Architecture decisions
```

## Install from GitHub with Hermes

Replace the placeholder with the actual club repository path; do not type `OWNER`, `REPOSITORY`, or `path/to` literally.

```bash
hermes skills inspect OWNER/REPOSITORY/path/to/automatic-anki-flashcard-creation
hermes skills install OWNER/REPOSITORY/path/to/automatic-anki-flashcard-creation
hermes skills list
```

For a local copy, place the complete directory—not only `SKILL.md`—under the active Hermes profile's `skills/` directory. The beginner guide includes macOS/Linux and Windows PowerShell examples.

## Validate the package

From the package root:

```bash
python -m unittest discover -s tests -v
python scripts/validate_package.py
python scripts/validate_export.py --tsv examples/Alpha.tsv --report examples/Alpha.validation.json --expected-count 2
```

The test suite covers eligible text and image candidates, ambiguous and malformed sources, all duplicate policies, identity isolation, atomic NATO naming, HTML escaping, configuration coherence, template rendering, and report validation.

## Security

Never commit bot tokens, model-provider credentials, personal configuration, Batch state, source screenshots, or generated exports. The Portable Core HTML-escapes source content, writes private artifacts with exclusive creation, and stores different identities in separate hashed directories.

Use allowlists or pairing, least-privilege bot permissions, and a dedicated test conversation or server.

## Scope

The initial package covers dictionary-entry screenshots, explicit text items, and separable lists. Handwriting, audio, arbitrary textbook pages, direct Anki database writes, automatic AnkiWeb imports, and Meta/Facebook Messenger are outside scope.

## License

MIT. See [`LICENSE`](LICENSE).

## Lab guardrails

The header describes the supplied offline Docker test service, not a live messaging deployment.
Run `docker compose build` then `docker compose run --rm demo` while attending output.
It uses synthetic fixtures, no network, no host mounts and disposable temporary storage.
No provider, bot, Anki account or local collection is connected by these tests.
Network adapters and remote interpretation require separate reviewed configuration and approval.
Review every export before importing. Never publish real screenshots, conversation identifiers,
configuration, batch state or generated personal exports. Installation guidance is supplied upstream;
live third-party compatibility has not been verified here. See LICENSE for retained attribution.
