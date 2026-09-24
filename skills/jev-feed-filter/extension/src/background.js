import { CATEGORIES, loadSettings, getApiKey } from "./settings.js";

const PRICE_PER_TOKEN = 0.042 / 1e6; // Jev input price, output is free
const CACHE_LIMIT = 5000;

const memCache = new Map(); // key -> { probs, ts }
const inflight = new Map(); // key -> Promise
const tabCounts = new Map(); // tabId -> filtered count
let active = 0;
const waiters = [];
let keyError = "";
let cacheLoaded = false;

// ---------- cache (survives service worker restarts within a browser session) ----------
async function loadCache() {
  if (cacheLoaded) return;
  cacheLoaded = true;
  const { verdicts } = await chrome.storage.session.get("verdicts");
  if (verdicts) for (const [k, v] of verdicts) memCache.set(k, v);
}
let persistTimer = null;
function persistCache() {
  clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    while (memCache.size > CACHE_LIMIT) memCache.delete(memCache.keys().next().value);
    chrome.storage.session.set({ verdicts: [...memCache.entries()] }).catch(() => {});
  }, 1500);
}

// ---------- stats (kept in memory, written through a queue so parallel updates never clobber) ----------
let stats = null;
let statsWrite = Promise.resolve();
async function getStats() {
  if (!stats) stats = (await chrome.storage.session.get("stats")).stats || freshStats();
  return stats;
}
async function bumpStats(patch) {
  const s = await getStats();
  for (const [k, v] of Object.entries(patch)) {
    if (k === "filtered") for (const [c, n] of Object.entries(v)) s.filtered[c] = (s.filtered[c] || 0) + n;
    else s[k] = (s[k] || 0) + v;
  }
  statsWrite = statsWrite.then(() => chrome.storage.session.set({ stats: s })).catch(() => {});
}
function freshStats() {
  return { checked: 0, calls: 0, cacheHits: 0, skipped: 0, tokens: 0, errors: 0, filtered: {} };
}

// ---------- concurrency ----------
async function acquire(max) {
  if (active < max) { active++; return; }
  await new Promise((r) => waiters.push(r));
  active++;
}
function release() {
  active--;
  const next = waiters.shift();
  if (next) next();
}

// ---------- text features (Jev is weak at counting, so we count for it) ----------
function textStats(text) {
  const lines = text.split(/\n/).filter((l) => l.trim());
  const letters = text.replace(/[^A-Za-z]/g, "");
  const caps = text.replace(/[^A-Z]/g, "");
  return {
    characters: text.length,
    lines: lines.length,
    em_dashes: (text.match(/—/g) || []).length,
    emojis: (text.match(/\p{Extended_Pictographic}/gu) || []).length,
    hashtags: (text.match(/(^|\s)#\w+/g) || []).length,
    lines_starting_with_emoji_or_bullet: lines.filter((l) => /^\s*(\p{Extended_Pictographic}|[-•▸→\d]+[.)]?\s)/u.test(l)).length,
    uppercase_letter_share: letters.length ? +(caps.length / letters.length).toFixed(2) : 0,
    links: (text.match(/https?:\/\/\S+/g) || []).length,
  };
}

function buildState(post) {
  const state = {
    platform: "X (Twitter) timeline",
    author: "@" + post.handle,
    post_text: post.text,
    text_stats: textStats(post.text),
  };
  if (post.isReply) state.is_reply = true;
  if (post.quoted) state.quoted_post_text = post.quoted.slice(0, 1200);
  if (post.hasMedia) state.has_image_or_video = true;
  return state;
}

// ---------- Jev call ----------
async function callJev(post, settings, apiKey) {
  const questions = {};
  for (const [id, cat] of Object.entries(CATEGORIES)) {
    if (!settings.categories[id]?.on) continue;
    questions[id] = { type: "noul", instructions: cat.instructions, criteria: cat.criteria };
  }
  if (!Object.keys(questions).length) return {};

  const body = JSON.stringify({ model: settings.model, state: buildState(post), questions });
  for (let attempt = 0; attempt < 4; attempt++) {
    const res = await fetch(settings.apiBase.replace(/\/$/, "") + "/v1/systemone", {
      method: "POST",
      headers: { Authorization: "Bearer " + apiKey, "Content-Type": "application/json" },
      body,
    });
    if (res.ok) {
      const data = await res.json();
      const probs = {};
      for (const id of Object.keys(questions)) probs[id] = data.answers?.[id]?.noul ?? 0;
      await bumpStats({ calls: 1, tokens: data.usage?.input_tokens || 0 });
      return probs;
    }
    if (res.status === 401 || res.status === 403) {
      keyError = "API key rejected (" + res.status + ")";
      await chrome.storage.session.set({ keyError });
      throw new Error(keyError);
    }
    if (res.status === 422) {
      throw new Error("Jev rejected request (422): " + (await res.text()).slice(0, 200));
    }
    if (res.status === 429 || res.status >= 500) {
      const ra = Number(res.headers.get("retry-after"));
      const wait = (ra > 0 ? ra * 1000 : 500 * 2 ** attempt) + Math.random() * 250;
      await new Promise((r) => setTimeout(r, wait));
      continue;
    }
    throw new Error("Jev HTTP " + res.status);
  }
  throw new Error("Jev unavailable after retries");
}

function decide(probs, settings) {
  let top = null;
  const flags = [];
  for (const [id, p] of Object.entries(probs)) {
    const cfg = settings.categories[id];
    if (!cfg?.on) continue;
    if (p >= cfg.threshold) {
      flags.push(id);
      if (!top || p > top.p) top = { id, label: CATEGORIES[id].label, p };
    }
  }
  return { flags, top };
}

async function classify(post, tabId) {
  const settings = await loadSettings();
  if (!settings.enabled) return { off: true };
  if (settings.allowlist.includes(post.handle.toLowerCase())) return { skip: "allowlist" };
  const combined = (post.text + " " + (post.quoted || "")).trim();
  if (combined.length < settings.minChars) {
    await bumpStats({ skipped: 1 });
    return { skip: "short" };
  }

  await loadCache();
  const cats = Object.keys(CATEGORIES).filter((c) => settings.categories[c]?.on).join(",");
  const key = settings.model + ":" + cats + ":" + post.id;
  let probs = memCache.get(key)?.probs;

  if (probs) {
    if (!post.recheck) await bumpStats({ cacheHits: 1 });
  } else {
    const apiKey = await getApiKey();
    if (!apiKey) return { error: "no-key" };
    if (keyError) return { error: keyError };
    if (!inflight.has(key)) {
      inflight.set(key, (async () => {
        await acquire(settings.maxConcurrent);
        try { return await callJev(post, settings, apiKey); }
        finally { release(); inflight.delete(key); }
      })());
    }
    try {
      probs = await inflight.get(key);
    } catch (e) {
      await bumpStats({ errors: 1 });
      return { error: String(e.message || e) };
    }
    memCache.set(key, { probs, ts: Date.now() });
    persistCache();
    await bumpStats({ checked: 1 });
  }

  const verdict = decide(probs, settings);
  if (verdict.top && tabId != null && !post.recheck) {
    tabCounts.set(tabId, (tabCounts.get(tabId) || 0) + 1);
    chrome.action.setBadgeText({ tabId, text: String(tabCounts.get(tabId)) });
    chrome.action.setBadgeBackgroundColor({ tabId, color: "#3b3a33" });
    const f = {};
    for (const id of verdict.flags) f[id] = 1;
    await bumpStats({ filtered: f });
  }
  return { probs, ...verdict, mode: settings.mode };
}

async function testKey(apiKey, apiBase) {
  const settings = await loadSettings();
  const res = await fetch((apiBase || settings.apiBase).replace(/\/$/, "") + "/v1/systemone", {
    method: "POST",
    headers: { Authorization: "Bearer " + apiKey, "Content-Type": "application/json" },
    body: JSON.stringify({
      model: settings.model,
      state: { post_text: "Like and repost if you agree!!! Follow for more." },
      questions: { bait: { type: "noul", instructions: CATEGORIES.bait.instructions, criteria: CATEGORIES.bait.criteria } },
    }),
  });
  const text = await res.text();
  if (!res.ok) return { ok: false, status: res.status, detail: text.slice(0, 300) };
  const data = JSON.parse(text);
  keyError = "";
  await chrome.storage.session.remove("keyError");
  return { ok: true, model: data.model, bait: data.answers?.bait?.noul };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const tabId = sender.tab?.id;
  (async () => {
    switch (msg.type) {
      case "classify": return classify(msg.post, tabId);
      case "test-key": return testKey(msg.apiKey, msg.apiBase);
      case "stats": {
        const s = await getStats();
        const { keyError: ke } = await chrome.storage.session.get("keyError");
        return { stats: s, keyError: ke || "", cost: +(s.tokens * PRICE_PER_TOKEN).toFixed(5) };
      }
      case "reset-key-error":
        keyError = "";
        await chrome.storage.session.remove("keyError");
        return { ok: true };
      case "clear-cache":
        memCache.clear();
        stats = freshStats();
        await chrome.storage.session.remove(["verdicts", "stats"]);
        return { ok: true };
      default: return { error: "unknown message" };
    }
  })().then(sendResponse, (e) => sendResponse({ error: String(e) }));
  return true; // async response
});

chrome.tabs.onRemoved.addListener((tabId) => tabCounts.delete(tabId));
