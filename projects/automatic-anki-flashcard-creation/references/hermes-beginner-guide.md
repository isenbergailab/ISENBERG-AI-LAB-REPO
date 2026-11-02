# Beginner Hermes and Skill Setup

This guide sets up Hermes Agent as the tested reference runtime. Finish the normal Hermes chat setup before adding Telegram or Discord.

## 1. What the parts mean

- **AI model**: the service that interprets the source and writes card content.
- **Model provider**: the company or local server that gives Hermes access to an AI model.
- **Agent**: software that lets the model use tools, files, and repeatable procedures.
- **Skill**: a directory containing instructions and supporting files for one workflow.
- **Language Profile**: the language-specific rules for the four Card Roles.
- **Message Adapter**: the connection between a conversation service and the card workflow.
- **Bot token**: a password that lets the gateway control a Telegram or Discord bot.
- **Local output directory**: a folder on the computer where this skill stores private Batch state and completed exports.

Never paste a model key or bot token into chat or commit one to Git.

## 2. Open a command window

- **macOS:** open **Applications → Utilities → Terminal**.
- **Windows:** open **PowerShell** from the Start menu.
- **Linux:** open your distribution's Terminal application.

Commands appear in code blocks. Type or paste one command, press Enter, and wait for it to finish before running the next. Text such as `OWNER`, `REPOSITORY`, or `<PROFILE>` is a placeholder that must be replaced; do not type the angle brackets.

## 3. Install Hermes

For macOS or Windows, use the signed desktop package from:

- <https://hermes-agent.nousresearch.com/>

For a CLI installation on macOS, Linux, or WSL2:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

For native Windows PowerShell:

```powershell
iex (irm https://hermes-agent.nousresearch.com/install.ps1)
```

Reload the shell after a CLI installation, then run:

```bash
hermes setup
hermes doctor
hermes
```

`hermes setup` configures the first provider and model. `hermes doctor` checks the installation. Do not continue to messaging until a normal Hermes conversation works.[1][2][3]

## 4. Optional isolated profile

A named profile keeps this bot's configuration, secrets, skills, sessions, and gateway state separate:

```bash
hermes profile create anki-bot
hermes -p anki-bot setup
hermes -p anki-bot doctor
```

Use the same `-p anki-bot` prefix for every related command. Ask Hermes for the profile's exact paths:

```bash
hermes -p anki-bot config path
hermes -p anki-bot config env-path
hermes profile show anki-bot
```

A profile separates Hermes state, but it is not a complete operating-system filesystem sandbox.[4]

## 5. Install the skill from GitHub

Replace `OWNER/REPOSITORY/path/to/automatic-anki-flashcard-creation` with the real path shown by the repository maintainer.

Inspect it before installation:

```bash
hermes skills inspect OWNER/REPOSITORY/path/to/automatic-anki-flashcard-creation
```

Install and verify it:

```bash
hermes skills install OWNER/REPOSITORY/path/to/automatic-anki-flashcard-creation
hermes skills list
```

For the named profile:

```bash
hermes -p anki-bot skills inspect OWNER/REPOSITORY/path/to/automatic-anki-flashcard-creation
hermes -p anki-bot skills install OWNER/REPOSITORY/path/to/automatic-anki-flashcard-creation
hermes -p anki-bot skills list
```

Hermes scans GitHub-installed skills. Do not use `--force` to bypass a dangerous verdict.[5][6]

## 6. Install a local development copy

Current Hermes documentation does not define a local-path form of `hermes skills install`. Copy the complete skill directory under the active profile's `skills/` directory instead.

Default profile:

```bash
mkdir -p ~/.hermes/skills/local/automatic-anki-flashcard-creation
cp -R /absolute/path/to/automatic-anki-flashcard-creation/. \
  ~/.hermes/skills/local/automatic-anki-flashcard-creation/
hermes skills list
```

Named profile:

```bash
mkdir -p ~/.hermes/profiles/anki-bot/skills/local/automatic-anki-flashcard-creation
cp -R /absolute/path/to/automatic-anki-flashcard-creation/. \
  ~/.hermes/profiles/anki-bot/skills/local/automatic-anki-flashcard-creation/
hermes -p anki-bot skills list
```

Do not copy only `SKILL.md`; the workflow depends on its references, scripts, profiles, and templates.[5][6]

On Windows PowerShell, first obtain the active profile's location with `hermes config path`, then use `New-Item` and `Copy-Item`:

```powershell
$Destination = "$HOME\.hermes\skills\local\automatic-anki-flashcard-creation"
New-Item -ItemType Directory -Force -Path $Destination
Copy-Item -Recurse -Force "C:\path\to\automatic-anki-flashcard-creation\*" $Destination
hermes skills list
```

For a named profile, replace the destination with the parent shown by `hermes -p anki-bot config path`, followed by `skills\local\automatic-anki-flashcard-creation`.

## 7. Create personal configuration

The core uses a profile-safe location. From the installed skill directory, run:

```text
python <skill-directory>/scripts/automatic_anki_core.py init --hermes-home <active-Hermes-home>
```

For the default profile, `<active-Hermes-home>` is normally `~/.hermes` on macOS/Linux or `$HOME\.hermes` in PowerShell. For a named profile, use the directory containing the `config.yaml` reported by `hermes -p PROFILE config path`.

This creates:

```text
<Hermes home>/automatic-anki-flashcard-creation/config.json
<Hermes home>/automatic-anki-flashcard-creation/state/
<Hermes home>/automatic-anki-flashcard-creation/exports/
```

Edit `config.json` and choose:

- `target_language`
- `explanation_language`
- `language_profile`
- the four Anki field mappings
- output directory
- duplicate policy
- adapter type

Built-in values such as `"language_profile": "japanese"` resolve from the installed skill. A custom profile path resolves relative to `config.json` unless it is absolute. Do not commit the personal file.

Validate it from any working directory:

```text
python <skill-directory>/scripts/automatic_anki_core.py check-config --config <config-path>
```

## 8. Test in normal chat

Start Hermes:

```bash
hermes
```

Send or attach one safe test item, then ask:

```text
Create one card with automatic-anki-flashcard-creation, then export the batch.
```

A successful run returns:

- `Alpha.tsv`
- `Alpha.validation.json`
- A verified card count
- A warning if generated or uncertain content needs review

The attachment syntax used by Hermes messaging surfaces is:

```text
MEDIA:/absolute/path/to/Alpha.tsv
```

## 9. Configure the messaging gateway

Only continue after normal chat works:

```bash
hermes gateway setup
hermes gateway run
```

For a persistent macOS or Linux service:

```bash
hermes gateway install
hermes gateway start
hermes gateway status
```

For a named profile:

```bash
hermes -p anki-bot gateway setup
hermes -p anki-bot gateway install
hermes -p anki-bot gateway start
hermes -p anki-bot gateway status
```

The setup wizard stores secrets in the active profile's secret environment file. Keep secret redaction enabled:

```bash
hermes config set security.redact_secrets true
```

For a named profile, add `-p anki-bot` after `hermes`.[7][8]

## 10. Choose an external adapter

- Telegram: follow `references/telegram-setup.md`.
- Discord: follow `references/discord-setup.md`.

Configure one adapter at a time and test it in a private conversation or dedicated test server before broader use.

## 11. Recover from common problems

### Hermes cannot see the skill

Confirm the complete directory is under the active profile, then run the matching profile's `skills list` command. Restart the current Hermes session because skill indexes may be loaded at session start.

### The gateway uses the wrong profile

Stop it and repeat every setup, install, start, and status command with the intended `-p PROFILE` argument.

### An attachment is mentioned but not uploaded

The final reply should contain an absolute host-readable path prefixed with `MEDIA:`. A path that exists only inside a container cannot be uploaded by the host gateway.[7]

### A secret appeared in chat or source control

Rotate it at the provider immediately, replace the local secret, remove it from logs or history where possible, and restart the gateway. Do not test the exposed value again.

## Sources

[1]: https://hermes-agent.nousresearch.com/docs/getting-started/installation
[2]: https://hermes-agent.nousresearch.com/docs/getting-started/quickstart
[3]: https://hermes-agent.nousresearch.com/docs/reference/cli-commands#hermes-doctor
[4]: https://hermes-agent.nousresearch.com/docs/user-guide/profiles
[5]: https://hermes-agent.nousresearch.com/docs/user-guide/features/skills
[6]: https://hermes-agent.nousresearch.com/docs/guides/work-with-skills
[7]: https://hermes-agent.nousresearch.com/docs/user-guide/messaging
[8]: https://hermes-agent.nousresearch.com/docs/user-guide/secrets
