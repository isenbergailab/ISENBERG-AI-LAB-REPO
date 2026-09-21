# Agent Instructions

Rules for AI coding tools (Claude Code, OpenCode, Codex, etc.) working in this repo.

## Repo Layout
- `projects/<name>/` holds full projects. Start from `templates/project/`.
- `skills/<name>/` holds reusable agent skills. Start from `templates/skill/`.
- Folder names: lowercase, numbers, hyphens only.

## Required
- Every new folder in `projects/` or `skills/` must contain a README.md or SKILL.md.
- Keep changes inside the contributor's own folder unless asked otherwise.
- Update the table in `projects/README.md` when adding a project.

## Never
- Commit API keys, tokens, passwords, or `.env` files.
- Commit personal, student, or client data. Use fake sample data only.
- Commit model weights or files over 50 MB. Link Hugging Face (`isenberg-ai-lab`) instead.
- Push directly to `main`. All changes go through a Pull Request.
- Edit other contributors' folders, `.github/`, or root files without explicit request.

## Before Opening a PR
- Confirm code runs.
- Fill out the PR template checklist.
