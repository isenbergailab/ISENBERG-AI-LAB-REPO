# Validation and Export Format

## TSV

An Export is UTF-8 text with:

- No header row by default
- One Card per physical line
- Exactly four fields per row
- Exactly three literal tab separators per physical line
- Prompt, Answer, Notes, Example order
- A controlled `<br>` separator for intended Example line breaks
- No byte-order mark, carriage returns, NUL characters, or TSV quoting

The Portable Core HTML-escapes all candidate text before writing. It inserts only the controlled `<br>` separator between Example lines, preventing submitted markup from becoming active Anki HTML.

Empty Notes values are allowed, but the third column remains present. Prompt and the Example line list must not be empty.

## Identity-scoped filename sequence

Each adapter/user or adapter/user/conversation identity receives its own output subdirectory. Inside that directory the Export Sequence is:

```text
Alpha.tsv ... Zulu.tsv
Alpha-2.tsv ... Zulu-2.tsv
Alpha-3.tsv ...
```

The Portable Core reserves both files with exclusive creation and retries after a collision. It never overwrites an existing artifact.

## Validation Report

The report is UTF-8 JSON named `<base>.validation.json` in the same directory as its TSV.

A one-card report has this structure:

```json
{
  "schema_version": 1,
  "export_filename": "Alpha.tsv",
  "card_count": 1,
  "language_profile": "Japanese vocabulary",
  "batch": {
    "adapter": "hermes",
    "identity": "hermes-exampleidentity01",
    "identity_scope": "user-and-conversation",
    "finalized_by": "explicit",
    "duplicate_policy": "reject",
    "duplicate_outcomes": []
  },
  "cards": [
    {
      "row": 1,
      "source_id": "message-123-image-1",
      "source_type": "image",
      "prompt": "食べる",
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
  ],
  "rejected_sources": []
}
```

Allowed provenance values are `source`, `reference`, and `generated`. Allowed source types are `text`, `image`, and `list-item`.

Put uncertain Card Roles in `uncertain_fields`. Set `review_required` to true when a human should inspect the item before import. Use `issues` for concise explanations.

`duplicate_outcomes` records every duplicate affected by `reject`, `merge`, or `allow`. `rejected_sources` records ineligible sources as objects containing a non-empty `source_id` and `reason`. Rejections do not increase `card_count`.

## Portable Core commands

The skill resolves `scripts/automatic_anki_core.py` from its own installed directory and invokes it by absolute path. The core discovers configuration in this order:

1. Explicit `--config` path
2. `AUTOMATIC_ANKI_CONFIG`
3. `$HERMES_HOME/automatic-anki-flashcard-creation/config.json`
4. `~/.hermes/automatic-anki-flashcard-creation/config.json`

Relative state and output paths resolve from the directory containing `config.json`. A built-in profile name such as `japanese` resolves from this skill's `language-profiles/` directory; a profile value ending in a file extension resolves relative to `config.json` unless absolute.

## Validator

Run from any working directory by using the installed skill's absolute path:

```text
python <skill-directory>/scripts/validate_export.py --tsv <Alpha.tsv> --report <Alpha.validation.json> --expected-count 2
```

The validator checks:

- NATO filename and same-directory companion report name
- Valid UTF-8 without a byte-order mark
- Exactly three separators and four fields per row
- No physical field newlines
- Non-empty Prompt and required expected row count
- Complete batch identity, finalization, and duplicate data
- One complete report item for every TSV row
- Source identifiers and source types
- Matching row prompts and valid provenance
- Structured rejected-source records

A successful file validation is necessary but does not prove linguistic accuracy. Review items marked `review_required` before import.
