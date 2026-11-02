# Discord Adapter Setup

Use this after Hermes works in normal chat. Begin in a private, test-only Discord server and channel.

## 1. Create a test server

In Discord, create a server for testing rather than using an active community. Keep it invite-only and create a dedicated text channel for the bot.[1][2]

## 2. Create the application and bot

1. Open the Discord Developer Portal: <https://discord.com/developers/applications>.
2. Choose **New Application**.
3. Name the application.
4. Open **Bot**.
5. Generate or reset the token and copy it once into a password manager or local secret configuration.

Never paste the bot token into chat, issues, screenshots, or Git. Discord may show a newly generated token only once.[3][4]

## 3. Enable required intents

On the application's **Bot** page, enable the intents required by the current Hermes Discord guide:

- Server Members Intent
- Message Content Intent

Message Content is required when the bot must inspect ordinary guild messages and attachments. A design based only on direct messages, slash commands, or bot mentions may reduce that requirement, but follow Hermes's documented setup for the reference adapter.[3][5]

## 4. Install with minimum permissions

Enable **Guild Install**. Use the `bot` scope and add `applications.commands` only when slash commands are needed.

Begin with:

- View Channels
- Send Messages
- Attach Files
- Read Message History when the adapter needs earlier messages
- Send Messages in Threads only when threads are supported
- Embed Links only when rich embeds are required

Do not grant Administrator. Restrict the bot to the dedicated test channel with channel permission overrides.[3][6]

## 5. Configure Hermes

Run:

```bash
hermes gateway setup
```

For a named profile:

```bash
hermes -p anki-bot gateway setup
```

Select Discord and enter the token locally. The wizard validates the token, checks intents, provides an invite URL, and can allowlist the owner.

The corresponding profile-local values include:

```dotenv
DISCORD_BOT_TOKEN=replace-with-secret
DISCORD_ALLOWED_USERS=284102345871466496
```

Do not place real values in repository files.[7]

## 6. Start and test

Start the gateway:

```bash
hermes gateway run
```

In a server channel, mention the bot unless the channel is explicitly configured for free responses. Send one safe test word or screenshot, export the batch, and confirm that Discord receives the TSV and Validation Report as native files.

## 7. Optional mention-free channel

To let the bot answer without a mention in one selected channel, configure that channel's numeric ID locally:

```dotenv
DISCORD_FREE_RESPONSE_CHANNELS=123456789012345678
```

Keep this list narrow. Do not enable mention-free behavior across an active server by default.[7]

## 8. Attachment limits

Hermes's current Discord guide documents a default inbound attachment cap of 32 MiB. Keep vocabulary images small and do not raise the cap without considering memory and storage use.[7]

The bot requires Send Messages and Attach Files to return TSV files. Read Message History is not required merely to receive new live events, but it is useful when the adapter fetches prior context.[6]

## 9. Token compromise

If the token is exposed:

1. Open **Developer Portal → Application → Bot**.
2. Choose **Reset Token**.
3. Replace the local Hermes secret.
4. Restart the gateway.
5. Remove the exposed value from logs or repository history where possible.

Do not continue using the old token.[3][4]

## 10. Data handling

Limit Discord data use to the stated adapter function and protect it from unauthorized access. Discord's Developer Policy places restrictions on using API-obtained message content for training machine-learning or AI models.[8]

## Sources

[1]: https://support.discord.com/hc/en-us/articles/204849977-How-do-I-create-a-server
[2]: https://support.discord.com/hc/en-us/articles/206143407-How-do-I-set-up-a-private-server
[3]: https://docs.discord.com/developers/quick-start/getting-started
[4]: https://support-dev.discord.com/hc/en-us/articles/6470840524311-Why-can-t-I-copy-my-bot-s-token
[5]: https://docs.discord.com/developers/events/gateway
[6]: https://docs.discord.com/developers/topics/permissions
[7]: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/discord
[8]: https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy
