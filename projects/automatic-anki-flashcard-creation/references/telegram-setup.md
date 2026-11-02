# Telegram Adapter Setup

Use this after Hermes works in normal chat. The simplest and safest starting point is a private chat with an allowlisted user.

## 1. Create the bot

1. In Telegram, open the verified `@BotFather` account.
2. Send `/newbot`.
3. Choose a display name and a username ending in `bot`.
4. Copy the token once and store it as a secret.

The token is the bot's password. Never paste it into an AI chat, screenshot, issue, README, or Git commit.[1][2]

## 2. Configure Hermes

Run:

```bash
hermes gateway setup
```

For a named profile:

```bash
hermes -p anki-bot gateway setup
```

Select Telegram, enter the token locally, and allowlist the intended user's numeric Telegram ID. A Telegram username is not the same as the numeric user ID.

The wizard manages profile-local values equivalent to:

```dotenv
TELEGRAM_BOT_TOKEN=replace-with-secret
TELEGRAM_ALLOWED_USERS=123456789
```

Do not place real values in repository files.[3]

## 3. Start and test

Start the gateway:

```bash
hermes gateway run
```

Open the bot in Telegram, select **Start**, and send one safe test word or screenshot. Telegram bots cannot initiate a normal private conversation until the user contacts them first.[1][4]

Ask the bot to export the batch. Confirm that Telegram receives both the TSV and Validation Report as native files.

## 4. Keep the bot private

For a private-chat-only bot:

- Keep `TELEGRAM_ALLOWED_USERS` limited to intended numeric user IDs.
- Use BotFather's `/setjoingroups` setting to prevent group additions when groups are unnecessary.
- Keep group Privacy Mode enabled.

Privacy Mode applies to groups, not private chats. An allowlist remains necessary for a private bot with a public username.[1][2]

## 5. Optional groups

Privacy Mode is enabled by default. A non-admin bot normally sees commands and messages directed to it, not every group message.[2]

Do not disable Privacy Mode unless the workflow genuinely needs ordinary group messages. After changing it, remove and re-add the bot. Do not make the bot an administrator merely as a troubleshooting shortcut.

## 6. File limits

Hermes uses Telegram's native file delivery. The hosted Bot API currently allows general outbound documents up to 50 MB and ordinary inbound bot downloads up to 20 MB. Vocabulary screenshots and TSV files should remain far below these limits.[5]

Do not log Telegram download URLs because they can contain the bot token.

## 7. Token compromise

If the token appears in chat, logs, or source control:

1. Open BotFather.
2. Generate a replacement with `/token` for the affected bot.
3. Replace the local Hermes secret.
4. Restart the gateway.
5. Remove the exposed value from logs or repository history where possible.

Do not delete the entire bot for an ordinary token leak; rotate the token.[1][2]

## Sources

[1]: https://core.telegram.org/bots
[2]: https://core.telegram.org/bots/features
[3]: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram
[4]: https://core.telegram.org/bots/tutorial#sending-messages
[5]: https://core.telegram.org/bots/api#sending-files
