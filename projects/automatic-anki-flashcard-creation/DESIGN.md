# Automatic Anki Flashcard Creation Design

Status: Accepted

## Purpose

Create a reusable skill package that turns language-learning material submitted through a messaging service into validated, manually importable Anki TSV batches. The package must support multiple languages and messaging services without coupling card-generation rules to one provider.

## Architecture

- A platform-neutral Portable Core accepts Eligible Sources and produces Cards, Exports, and Validation Reports.
- Message Adapters connect Hermes, Telegram, Discord, or future messaging services to the Portable Core.
- Language Profiles define language-specific extraction, field content, formatting, and example rules.
- The repository contains a portable `SKILL.md`, configuration schema, templates, tests, adapter guides, and a tested Hermes Reference Implementation. It is not an always-on universal messaging bot.

## Card model

Every Card contains four ordered semantic roles:

1. Prompt
2. Answer
3. Notes
4. Example

Users map these roles to the fields of their own Anki note type. Field names do not need to match the role names.

Example profiles:

- Japanese: written form; meaning; reading and relevant usage notes; example plus translation.
- Spanish: word or phrase; meaning; gender, part of speech, or pronunciation notes; example plus translation.
- Mandarin: written form; meaning; pinyin and usage notes; example plus translation.

Each configuration explicitly declares the target language, explanation language, Language Profile, Anki field mapping, and export behavior. Automatic language detection may assist setup but is not authoritative.

## Eligible inputs

The first release accepts:

- One dictionary-entry screenshot representing one study item.
- One clearly identified word or phrase in text.
- An explicitly separable list of study items.

The first release rejects or requests clarification for ambiguous screenshots, arbitrary textbook pages, handwriting, audio, and images containing multiple inseparable entries.

## Source and enrichment policy

The submitted source is authoritative. External dictionaries or generated examples may fill missing Card content. Uncertain, externally sourced, and generated content is identified in the Validation Report rather than inserted as citations into the four Anki fields.

## Batch and export lifecycle

- A Batch is scoped to one user and collects Cards awaiting export.
- Explicit export is authoritative.
- A configurable inactivity timeout may optionally finalize a Batch.
- Each Export contains only its Batch unless the user requests otherwise.
- Filenames progress through user-scoped NATO alphabet names such as `Alpha.tsv`, `Bravo.tsv`, and `Charlie.tsv`.
- Filename creation must prevent overwriting or collisions.
- Duplicate detection applies within the current Batch.

## Initial adapters

- Hermes Message Adapter: reference implementation and primary supported path.
- Telegram Bot: documented external adapter.
- Discord Bot: documented external adapter.

The term “Messenger” is not used as a provider name because it could be confused with Meta/Facebook Messenger. Future services participate by implementing the published Message Adapter contract.

## Delivery and storage

A completed Export is returned as a downloadable attachment through the originating Message Adapter. Users may additionally configure a local output directory. Direct manipulation of Anki databases is outside scope; users manually import validated TSV files into Anki Desktop and then sync them to AnkiWeb or mobile clients.

## Validation contract

Before delivery, the implementation verifies:

- Every included Card corresponds to an Eligible Source.
- Every row contains exactly four ordered fields.
- Fields contain no unsafe literal tabs or unintended row breaks.
- The exported row count equals the finalized Card count.
- Duplicate handling followed the configured policy.
- The filename is the next collision-safe name in that user’s Export Sequence.
- Every uncertain or enriched value appears in the Validation Report.

Ambiguous source material produces no Card until clarified.

## Security and privacy

- Adapter credentials and secrets remain outside the repository.
- Setup guides request the minimum permissions required by each provider.
- Configuration examples contain placeholders rather than live identifiers or tokens.
- Local output paths and user data are not committed.
- The Portable Core does not directly modify Anki’s database.

## Beginner onboarding standard

The primary guide assumes no previous experience with Anki, AI agents, command-line tools, bots, or API credentials. It must explain each step in plain language and define unfamiliar terms before using them.

The Anki section must cover:

- The difference between notes, cards, fields, card templates, decks, and AnkiWeb synchronization.
- Installing Anki Desktop and optionally creating an AnkiWeb account for synchronization.
- Creating or cloning a note type with four fields mapped to Prompt, Answer, Notes, and Example.
- Installing a supplied front template, back template, and styling block, with a preview of the resulting card.
- Importing a UTF-8 tab-separated file, selecting the correct note type and deck, mapping all four columns, enabling HTML so `<br>` creates a line break, previewing the import, and selecting the intended duplicate behavior.
- Synchronizing the imported cards to AnkiWeb and mobile devices.
- Troubleshooting field-order mistakes, visible `<br>` text, encoding problems, duplicate warnings, and cards displaying the wrong side.

The core TSV workflow requires no Anki add-ons. Any language-specific or convenience add-ons must appear in a separate optional section that explains their purpose, installation steps, supported Anki versions, permissions or risks, and how to remove them. The core workflow must continue to work when no optional add-ons are installed.

The agent section must cover:

- What an AI agent, skill, model provider, Message Adapter, bot token, and local output directory mean.
- Installing and testing the Hermes reference runtime from an empty setup.
- Installing the skill and selecting a Language Profile.
- Configuring one Message Adapter at a time, beginning with the Hermes Message Adapter.
- Creating Telegram or Discord bot credentials through the provider’s official setup flow without pasting secrets into chat or committing them to Git.
- Sending a safe test item, exporting the first Batch, locating the TSV and Validation Report, and recovering from common setup failures.

## Required repository contents

- Portable `SKILL.md`
- Configuration schema and example configurations
- Language Profile specification and templates
- Japanese, Spanish, and Mandarin sample profiles
- Message Adapter contract
- Hermes reference implementation
- Telegram setup and adapter guide
- Discord setup and adapter guide
- Beginner Anki Desktop installation, four-field note-type, card-template, TSV import, and AnkiWeb synchronization guide
- Beginner AI-agent, skill installation, credentials, and messaging setup guide
- Optional add-ons guide that clearly states no add-on is required for the core workflow
- Automated fixtures for images, text, malformed input, duplicates, field validation, and filename progression
- Glossary and architectural decision records

## Release acceptance

The first release is ready only when a person with no prior Anki or AI-agent experience can follow the documentation from an empty setup to:

1. Install Anki Desktop and understand how it relates to AnkiWeb.
2. Create a four-field note type and install the supplied card templates and styling.
3. Install and test the Hermes reference runtime.
4. Install the skill and select a Language Profile.
5. Configure at least one supported Message Adapter without committing credentials.
6. Submit valid image and text sources.
7. Receive a validated NATO-named TSV and its Validation Report.
8. Import the TSV with all four roles mapped correctly and sync the result to AnkiWeb.
9. Observe safe rejection of malformed or ambiguous input.

## Deferred capabilities

- Meta/Facebook Messenger unless separately implemented and tested
- Handwriting recognition
- Audio transcription
- Arbitrary textbook-page segmentation
- Direct Anki database writes
- Automatic AnkiWeb import
- Claims of compatibility with untested messaging services
