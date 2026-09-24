// Shared settings for background, options and popup.
// The API key lives in storage.local (never synced to your Google/Microsoft account).

export const CATEGORIES = {
  ragebait: {
    label: "Ragebait",
    instructions:
      "Is this post written mainly to provoke anger or outrage so people engage with it?",
    criteria: {
      true:
        "Yes. Manufactured outrage: inflammatory generalizations about a group, deliberately bad-faith or contrarian hot takes, dunking or quote-tweet bait, culture-war provocation, exaggerating a minor event to make people furious, insults aimed at getting a reaction.",
      false:
        "No. It informs, reports news, argues in good faith, jokes, criticizes something specific, or shares a personal update. A political or serious topic, or a critical tone, is not ragebait by itself.",
    },
  },
  slop: {
    label: "AI slop",
    instructions:
      "Is this post low-effort, AI-generated or AI-templated filler rather than something a person actually wrote with a point?",
    criteria: {
      true:
        "Yes. Generic engagement-farming text: listicle hooks ('Here are 7 tools that...'), 'I asked ChatGPT/Claude...', hollow hustle or motivation posts, formulaic phrasing ('It's not X, it's Y', 'Let that sink in', 'Read that again', 'This changes everything'), emoji bullet lists, heavy em dash use, vague threads with no specifics, reply-bot comments that just restate the original post, AI-generated image bait captions.",
      false:
        "No. Specific, personal, opinionated or original writing by a real person, including short casual posts, jokes, questions, news, or technical detail with concrete facts.",
    },
  },
  bait: {
    label: "Engagement bait",
    instructions:
      "Does this post explicitly fish for likes, reposts, follows, replies or bookmarks?",
    criteria: {
      true:
        "Yes. 'Like if you agree', 'Comment YES and I'll DM you', 'Follow for more', 'Bookmark this', giveaway or follow-trains, 'Most people won't understand this', 'Only real ones will repost', unfinished cliffhangers that exist only to farm replies.",
      false: "No. It does not ask the reader to engage for its own sake.",
    },
  },
};

export const DEFAULTS = {
  enabled: true,
  apiBase: "https://api.typesafe.ai",
  model: "jev-1.13.0", // pinned so thresholds stay stable across Jev upgrades
  mode: "collapse", // collapse | hide | label
  categories: {
    ragebait: { on: true, threshold: 0.8 },
    slop: { on: true, threshold: 0.85 },
    bait: { on: true, threshold: 0.85 },
  },
  minChars: 30, // skip tiny posts, not worth a call
  allowlist: [], // handles never filtered, lowercase, no @
  maxConcurrent: 6,
};

export async function loadSettings() {
  const { settings } = await chrome.storage.sync.get("settings");
  return mergeDeep(structuredClone(DEFAULTS), settings || {});
}

export async function saveSettings(patch) {
  const current = await loadSettings();
  const next = mergeDeep(current, patch);
  await chrome.storage.sync.set({ settings: next });
  return next;
}

export async function getApiKey() {
  const { apiKey } = await chrome.storage.local.get("apiKey");
  return apiKey || "";
}

export async function setApiKey(apiKey) {
  await chrome.storage.local.set({ apiKey: apiKey.trim() });
}

function mergeDeep(target, src) {
  for (const [k, v] of Object.entries(src)) {
    if (v && typeof v === "object" && !Array.isArray(v)) {
      target[k] = mergeDeep(target[k] && typeof target[k] === "object" ? target[k] : {}, v);
    } else {
      target[k] = v;
    }
  }
  return target;
}
