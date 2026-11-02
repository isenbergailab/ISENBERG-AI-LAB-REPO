# Language Profile Specification

A Language Profile defines how source content becomes the four Card Roles. It must not contain messaging credentials, output paths, or user identity.

## Required properties

- `name`: human-readable profile name.
- `target_language`: BCP 47 language tag when practical.
- `explanation_language`: language used for meanings and translations.
- `roles`: instructions for Prompt, Answer, Notes, and Example.
- `rules`: ordered, language-specific extraction and formatting rules.

Optional properties:

- `example_separator`: normally `<br>`.
- `approved_reference_domains`: domains allowed for enrichment.

Validate profile structure against `language-profiles/schema.json`.

## Design rules

1. **Keep roles semantic.** Do not assume Anki fields are literally named Front, Back, Notes, and Example.
2. **Keep language dimensions coherent.** Reading systems, noun gender, classifiers, and transliteration belong in Notes only when meaningful for that language.
3. **Separate source from enrichment.** The profile defines preferred references, but the Validation Report records whether values came from the source, a reference, or generation.
4. **Define example quality.** Require a complete natural sentence and complete translation unless the language pedagogy intentionally differs.
5. **Define normalization narrowly.** Preserve source spelling and diacritics unless the profile explicitly defines script conversion or lemma normalization.
6. **Avoid hidden add-on dependencies.** If a profile benefits from an Anki add-on, mark it optional and provide a no-add-on path.

## Creating a profile

1. Copy an existing profile or create a JSON file matching the schema.
2. Choose one target language and one explanation language.
3. Define the exact content of each role.
4. Add rules for morphology, pronunciation, meaning grouping, examples, and scripts.
5. List only references suitable for the language.
6. Create at least three fixtures: ordinary item, ambiguous item, and item missing an example.
7. Verify output has exactly four fields and all enrichment is reported.

## Review checklist

- Prompt is a stable lookup identity.
- Answer is concise enough for recall.
- Notes contains language-specific support, not a second unstructured answer.
- Example includes the target item naturally.
- Translation is complete and uses the explanation language.
- Profile rules do not contradict each other.
- No provider credentials or machine-local paths appear in the profile.
