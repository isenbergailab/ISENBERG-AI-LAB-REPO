# Privacy and publication boundaries

Public package contains code and fictional fixtures. Original settings, state, credentials,
private notes, exports, backups, planner documents, assets, and source Git history are excluded.
Only reviewed file types were copied. Publication manifests use sanitized file hashes.

## Runtime data

The agent can store capture text, proposals, approved learning examples, feedback, mail extracts,
backup bytes, and undo records. These live in configured state and generated vault notes.
Runtime data is not anonymized. Retention has no universal automatic expiry.
Stop agent before inspecting or deleting disposable state. Deleting undo records loses recovery.
Never connect real accounts during lab demo.

## Remote inference

Default remote inference is false. Supplied Docker demo has networking disabled.
Enabling inference sends eligible input and relevant context to selected model provider.
Provider privacy request fields are requests, not independently verified guarantees.
Never enable paid calls without separate approval and provider-side spending cap.
Model costs and contracts remain unverified.

## Screening

`#private`, `private: true`, and secret screening protect marked content.
Private markers require correct use. Sensitive mail screening can produce false positives and misses.
No filter proves that arbitrary input contains no personal information.

`[mail] restricted_domains` lists organizations excluded from mail processing.
It checks sender domains, subdomains, and email addresses embedded in message text.
Empty list is default. Add actual restrictions only to ignored local configuration.
Additional `private_senders` and `private_words` remain configurable.
Generic category lists mention public service domains; they indicate screening categories,
not accounts owned by any contributor.

## Approvals and writes

Judgment proposals require approval by default. Hash checks reject stale proposals.
Some upkeep writes automatically and records Undo. Preview and support notes may be generated.
Keep entire writable vault and state disposable. Human-authored originals never belong in this demo.
Optional mail code may create drafts with approval; it never sends messages.
The isolated demo enables no mail, calendar, or Telegram accounts.

## Public submissions

Run `python tools/check_public_files.py` before staging files.
Keep local configuration, vaults, credentials, and state excluded.
Use filename allowlists and review diff. Ignore patterns alone cannot remove tracked content.
Review new text for names, addresses, affiliations, private URLs, and copied real examples.
Synthetic test credentials are intentionally fake. TLS keys generate in temporary directories.
Never include existing source repository history in contribution.
Future Git commits may include public author metadata; it is separate from project content.
