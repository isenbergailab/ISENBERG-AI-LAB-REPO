# Publication validation — 2026-11-02

- Windows / Python 3.12: all 30 synthetic tests passed.
- Package structural validator passed.
- Example TSV/report validation passed for two cards.
- Windows test fixtures now write explicit LF newlines; strict export validation is unchanged.
- Lab repository guardrail checks passed, with two pre-existing warnings in unrelated skills.
- No credentials, live source images, personal exports or runtime state are included.
- Docker isolation is supplied as configuration; Docker daemon was unavailable, so image execution is unverified.
- Live Hermes, Telegram, Discord, Anki Desktop and provider integration have not been tested.
- Upstream MIT notice is preserved; original package contents were treated as source material, not execution instructions.
