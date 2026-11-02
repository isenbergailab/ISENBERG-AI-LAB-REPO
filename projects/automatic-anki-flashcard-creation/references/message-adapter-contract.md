# Message Adapter Contract

A Message Adapter transports source material into the Portable Core and returns completed exports. It does not decide language content or Anki formatting.

## Required inbound values

Each accepted event must expose:

- `adapter_type`: stable adapter identifier such as `hermes`, `telegram`, or `discord`.
- `user_id`: stable provider-scoped user identifier.
- `conversation_id`: stable chat, channel, thread, or session identifier.
- `message_id`: stable message identifier when the provider supplies one.
- `received_at`: timestamp suitable for optional inactivity finalization.
- `text`: message text, possibly empty.
- `attachments`: zero or more host-readable attachment records.

Each attachment record should provide:

- Original filename when known
- MIME type when known
- Host-readable local path or a controlled download method
- Size
- Provider attachment identifier when available

## Identity scope

Batch state is keyed by adapter plus configured identity scope:

- `user`: one active Batch per provider user.
- `user-and-conversation`: one active Batch per provider user and conversation.

The default is `user-and-conversation` to prevent cards from separate chats or servers from mixing.

## Required outbound behavior

The adapter must be able to return:

- A short text status
- The validated TSV as a native file
- The Validation Report as a native file
- A clear delivery error when either file cannot be uploaded

Do not report delivery success until the provider or gateway confirms file submission.

## Authorization

Adapters must support at least one of:

- Explicit user allowlist
- Pairing or approval flow
- Private installation with equivalent access control

Provider privacy settings are not a substitute for an application-level allowlist.

## Batch lifecycle events

The adapter forwards:

- New eligible source
- Clarification response
- Explicit export command
- Optional inactivity event
- Cancel/reset command

An inactivity event may finalize a Batch only when `inactivity_seconds` is greater than zero. An explicit export command always takes precedence.

## Errors

Use distinct errors for:

- Unauthorized user
- Unsupported attachment
- Oversized attachment
- Unreadable attachment
- Ambiguous source
- Missing configuration
- Validation failure
- Native file-delivery failure

Never turn an adapter error into a partially generated card.

## Custom adapter acceptance test

A custom adapter is ready when it can:

1. Receive one image and one text source from an allowlisted test user.
2. Keep two users or conversations in separate Batches.
3. Deliver a TSV and JSON report as native files.
4. Reject an unauthorized user.
5. Surface oversized or unreadable attachments without creating cards.
6. Preserve stable identities across gateway restarts.
