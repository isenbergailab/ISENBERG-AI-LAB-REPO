# Automatic Anki Flashcard Creation

Shared language for a messaging-driven workflow that turns submitted learning material into importable Anki card batches.

## Language

**Card**:
A single study item containing four ordered roles: Prompt, Answer, Notes, and Example.
_Avoid_: Flashcard row, entry

**Card Role**:
One of the four semantic positions in a Card, independent of the field names used by a particular Anki note type.
_Avoid_: Fixed field name

**Language Profile**:
The language-specific rules that determine what belongs in each Card Role.
_Avoid_: Language preset, translation template

**Message Adapter**:
The boundary through which a messaging service supplies source material and receives completed exports.
_Avoid_: Messaging integration, bot

**Hermes Message Adapter**:
The Message Adapter for conversations delivered through a Hermes Agent runtime, independent of the external transport carrying the conversation.
_Avoid_: Messenger, Facebook Messenger

**Batch**:
An uninterrupted, user-scoped collection of Cards awaiting export together.
_Avoid_: File, session

**Export**:
A finalized tab-separated package containing one Batch in the configured Card Role order.
_Avoid_: Deck, database

**Export Sequence**:
The user-scoped progression of NATO alphabet names assigned to successive Exports.
_Avoid_: Global counter, letter number

**Portable Core**:
The service-independent card-creation behavior shared by every Message Adapter and Language Profile.
_Avoid_: Universal bot

**Reference Implementation**:
A fully tested realization of the Portable Core for a specific agent runtime and supported Message Adapters.
_Avoid_: Guaranteed universal integration

**Eligible Source**:
Submitted material containing one clearly identifiable study item, or a list whose items can be separated without interpretation.
_Avoid_: Any attachment, arbitrary page

**Enrichment**:
Information added from outside the submitted source when required Card content is missing.
_Avoid_: Source extraction

**Validation Report**:
A companion record identifying uncertain, generated, or externally enriched Card content before import.
_Avoid_: Notes field, citation field
