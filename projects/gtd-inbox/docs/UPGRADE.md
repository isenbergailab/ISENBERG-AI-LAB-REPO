# Capability comparison and upgrade notes

Compared with the October public snapshot, this contribution includes:

| Area | Added behavior |
|---|---|
| Project views | Queued steps appear separately from selected next actions; TODAY and NEXT_ACTIONS refresh consistently. |
| Document intake | Complete tagged Markdown checklists retain dates, waits, anchors and completed state. |
| Capture | Source-linked Quick Update captures, completed-task context and project log entries. |
| Birthdays | Local calendar scanning proposes recurring reminders; it never sends birthday messages. |
| Planning | Day time blocks, workload estimates and generated calendar export. |
| Mail | Approved reply drafts, outbox retry handling and calendar-conflict previews. |
| Telegram | Optional relay and proactive pings with quiet hours. |
| Trust | Trust reporting and explicitly configured automatic proposal categories. |
| Models | Optional local/private endpoint routing and bounded repair of malformed responses. |
| Reference links | Reviewed links for a generic TOPIC folder and TOPIC_INDEX note. |
| Study review | Local image-based spaced reviews, grading, archive and Undo. |

## Commands and configuration

Use `python run.py --help` for available commands. `python run.py birthdays scan`
creates a reviewable proposal from the configured read-only calendar. Optional
`--choose-date "SamplePerson=MM-DD"` resolves an ambiguous birthday.

The public example retains `[mail] restricted_domains` screening for senders and
embedded addresses. Supply restrictions only in ignored local configuration.
`[telegram] pings = []`, `[trust] bookkeeping = []`, `[private_model] enabled = false`
and `[privacy] remote_inference = false` keep optional actions disabled.
Enabling Telegram pings permits outbound messages; review that action separately.
A zero software budget is not a provider hard cap. Paid inference requires a provider cap.

Reference linking operates only on `04_REFERENCE/TOPIC/TOPIC_INDEX.md` and public
notes beneath `04_REFERENCE/TOPIC`. This generalizes the source deployment's
subject-specific pilot; arbitrary reference folders are not supported yet.

## Optional Obsidian plugins

Test in a disposable vault. Copy each plugin's main.js, manifest.json and styles.css
to `.obsidian/plugins/<manifest-id>/`, then explicitly enable it in Obsidian.
Quick Update writes captures into INBOX; the agent later processes those captures.
Study Question Review modifies its dedicated study notes when its buttons are used.
It does not send study material to models. Rebuild its main.js with
`node obsidian-plugin/study-review/build.cjs` after editing core.js or main.src.js.
Run `node --test obsidian-plugin/study-review/test.cjs obsidian-plugin/study-review/integration.test.cjs obsidian-plugin/gtd-quick-update/test.cjs`.
Automated plugin tests do not establish live Obsidian compatibility.

## Publication boundaries

Source was copied into a separate staging directory. Personal runtime configuration,
state, credentials, notes, logs, images, exports and source Git history are excluded.
Examples use invented identities and reserved email domains. Source-specific names,
affiliations, subjects and task references were generalized. Existing public privacy
screening and temporary TLS certificate generation are preserved.

Review this pull request before use. No production deployment or migration occurred.
