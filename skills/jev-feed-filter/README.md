# jev-feed-filter

Browser extension that collapses ragebait, AI slop, and engagement bait on X. [TypeSafe](https://typesafe.ai/)'s Jev makes the call.

**Author:** Tdepth
**Status:** Working. Tested in Chromium against a fake Jev server and a mock X timeline.

## What It Does

Every post on your X timeline gets one Jev request with three yes/no (**Noul**) questions:

| Category | Catches |
|---|---|
| Ragebait | Manufactured outrage, bad-faith hot takes, dunk bait, culture-war provocation |
| AI slop | Listicle hooks, "Let that sink in", hollow hustle posts, emoji bullet threads, reply-bot filler |
| Engagement bait | "Like if you agree", "Comment YES", follow trains, "Bookmark this" |

Jev returns a probability for each. Over your threshold, the post collapses to one line with the reason and a
**Show** button. You can also hide posts fully or only label them.

Cost: about 350 Jev tokens per post. Roughly $0.015 per 1,000 posts.

## How to Run

Works in Brave, Chrome, Edge, and other Chromium browsers.

1. Download or clone this repo.
2. Open `brave://extensions` (Chrome: `chrome://extensions`, Edge: `edge://extensions`).
3. Turn on **Developer mode**.
4. Click **Load unpacked** and pick the `skills/jev-feed-filter/extension` folder.
5. Click the extension icon, then **Settings**. Paste your TypeSafe key from https://console.typesafe.ai/.
   Click **Save**, then **Test**.
6. Open x.com.

### Try it without a key

```powershell
py -3 skills/jev-feed-filter/tests/mock_jev.py     # macOS/Linux: python3 ...
```

In Settings, set the API key to `sk-test` and the API base URL to `http://127.0.0.1:8787`. Click **Save** and allow
the permission. The fake server flags posts by keyword only.

## Tuning

1. Start in **Label only** mode for a day.
2. Note false positives and misses.
3. Raise a threshold to cut false positives. Lower it to catch more.
4. Still wrong? Edit that category's `criteria` text in `extension/src/settings.js`, then reload the extension.

Changing thresholds or mode re-applies at once from cached verdicts. It makes no new API calls.

## How It Works

- `extension/src/content.js` finds posts on x.com, reads text (with emoji), quoted posts, and the author.
  It asks the background worker for a verdict and collapses, hides, or labels the post.
- `extension/src/background.js` calls Jev, runs up to 6 requests at once, retries on 429/5xx, and caches
  verdicts per post for the browser session.
- Jev isn't good at counting, so the extension counts for it. It sends em dashes, emojis, hashtags,
  bullet-style lines, and uppercase share as `text_stats` alongside the post.
- `extension/src/settings.js` holds the question text, criteria, and defaults.

Posts under 30 characters are skipped. Accounts on your allowlist are skipped. A post you open directly is never
filtered.

## Tools Used

- TypeSafe Jev (`jev-1.13.0`, pinned) through `POST https://api.typesafe.ai/v1/systemone`
- Chrome Manifest V3. Plain JavaScript, no build step, no dependencies.

## Privacy

- Your API key stays in the extension's local storage on your device. It never syncs.
- Post text goes to TypeSafe's API for classification. Nothing else leaves your browser.

## Notes

- X changes its page markup often. If filtering stops, the selectors in `content.js` (`article[data-testid="tweet"]`,
  `[data-testid="tweetText"]`) are the first thing to check.
- Images and video aren't classified. Jev takes text only.
- Stats in the popup reset when the browser closes.

## License

MIT. See [LICENSE](LICENSE).
