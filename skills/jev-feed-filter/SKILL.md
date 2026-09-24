---
name: jev-feed-filter
description: >
  Install, tune, or troubleshoot jev-feed-filter, a Chromium browser extension that uses TypeSafe Jev to
  collapse ragebait, AI slop, and engagement bait on X (Twitter). WHEN: "filter ragebait on X", "hide AI
  slop from my feed", "install the feed filter", "too many good posts are getting hidden", "the filter
  stopped working on X", "add a new category to the feed filter".
---

# jev-feed-filter

**Author:** Tdepth

## Purpose

Help a user install the extension, tune what it filters, and fix it when X changes its page.

## When to Use

- User wants a cleaner X timeline without ragebait, AI-generated filler, or engagement bait.
- User reports false positives (good posts collapsed) or misses (junk getting through).
- Filtering stopped after an X update.
- User wants a new category, such as crypto shilling or sports spoilers.

## Instructions

Resolve this skill's folder. The loadable extension is its `extension/` subfolder.

1. **Install.** Walk the user through their browser's extensions page (`brave://extensions`,
   `chrome://extensions`, `edge://extensions`), Developer mode, then **Load unpacked** on `extension/`.
   The user pastes their TypeSafe key into the extension's Settings. Never ask for the key in chat, and never
   write it into any file.
2. **Tune thresholds first.** Suggest **Label only** mode for a day. Too many good posts caught: raise that
   category's threshold by 0.03 to 0.05. Too much junk getting through: lower it. Thresholds live in Settings.
3. **Then tune wording.** If thresholds can't fix it, edit that category's `criteria.true` and `criteria.false`
   text in `extension/src/settings.js`. Add concrete examples of what was misjudged. Jev reads literally, so
   avoid negations inside a criterion. The user reloads the extension afterward.
4. **Add a category.** Add an entry to `CATEGORIES` and `DEFAULTS.categories` in `settings.js`, a label in
   `LABELS` in `content.js`, and a row in `popup.html`. Each category is one Noul question in the same request,
   so cost barely changes.
5. **Broken after an X update.** Check that `article[data-testid="tweet"]`, `[data-testid="tweetText"]` and
   the `a[href*="/status/"] time` permalink still exist on x.com, and update `readPost()` in `content.js`.
6. **Offline check.** Run `tests/mock_jev.py` and point Settings at `http://127.0.0.1:8787` with key `sk-test`.

## Examples

User: "It keeps hiding my friend's sarcastic posts as ragebait."
Agent: suggests adding the friend's handle to the allowlist in Settings. If it's a pattern, adds "sarcasm or
irony between friends" to the ragebait `criteria.false` text.

User: "Add a filter for crypto shilling."
Agent: adds a `shill` Noul category with concrete true/false criteria, default threshold 0.85, updates the
label map and popup, and tells the user to reload the extension.

## Requirements

- A Chromium browser (Brave, Chrome, Edge)
- `TYPESAFE_API_KEY` from https://console.typesafe.ai/, pasted into the extension's Settings
