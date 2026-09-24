import { CATEGORIES, loadSettings, saveSettings, getApiKey, setApiKey } from "./settings.js";

const $ = (id) => document.getElementById(id);

function status(el, text, ok) {
  el.textContent = text;
  el.className = "status " + (ok === true ? "ok" : ok === false ? "bad" : "");
}

async function render() {
  const s = await loadSettings();
  $("apiKey").value = await getApiKey();

  const wrap = $("cats");
  wrap.textContent = "";
  for (const [id, cat] of Object.entries(CATEGORIES)) {
    const cfg = s.categories[id];
    const row = document.createElement("div");
    row.className = "cat";
    row.innerHTML = `
      <input type="checkbox" id="on-${id}">
      <label for="on-${id}"><strong>${cat.label}</strong></label>
      <span class="val num" id="val-${id}"></span>
      <span></span><input class="slider" type="range" min="0.5" max="0.99" step="0.01" id="th-${id}" aria-label="${cat.label} threshold"><span></span>
      <span></span><span class="desc">${cat.criteria.true.replace(/^Yes\. /, "")}</span>`;
    wrap.append(row);
    const on = row.querySelector(`#on-${id}`);
    const th = row.querySelector(`#th-${id}`);
    const val = row.querySelector(`#val-${id}`);
    on.checked = cfg.on;
    th.value = cfg.threshold;
    val.textContent = Math.round(cfg.threshold * 100) + "%";
    th.addEventListener("input", () => (val.textContent = Math.round(th.value * 100) + "%"));
    // save immediately so an open X tab re-applies live
    th.addEventListener("change", () => saveSettings({ categories: { [id]: { threshold: +th.value } } }));
    on.addEventListener("change", () => saveSettings({ categories: { [id]: { on: on.checked } } }));
  }

  for (const r of document.querySelectorAll('input[name="mode"]')) {
    r.checked = r.value === s.mode;
    r.onchange = () => saveSettings({ mode: r.value });
  }
  $("allowlist").value = s.allowlist.map((h) => "@" + h).join("\n");
  $("model").value = s.model;
  $("minChars").value = s.minChars;
  $("apiBase").value = s.apiBase;
  $("maxConcurrent").value = s.maxConcurrent;
}

$("saveKey").onclick = async () => {
  await setApiKey($("apiKey").value);
  await chrome.runtime.sendMessage({ type: "reset-key-error" });
  status($("keyStatus"), "Saved.", true);
};

$("testKey").onclick = async () => {
  status($("keyStatus"), "Testing...");
  const apiBase = $("apiBase").value.trim();
  if (!(await ensureHostPermission(apiBase))) return status($("keyStatus"), "Permission for that API base was refused.", false);
  try {
    const r = await chrome.runtime.sendMessage({ type: "test-key", apiKey: $("apiKey").value.trim(), apiBase });
    if (r.ok) status($("keyStatus"), `Works. ${r.model} rated a bait sample at ${Math.round(r.bait * 100)}%.`, true);
    else status($("keyStatus"), `Failed (${r.status || "error"}): ${r.detail || r.error || ""}`, false);
  } catch (e) {
    status($("keyStatus"), "Failed: " + e.message, false);
  }
};

$("save").onclick = async () => {
  const apiBase = $("apiBase").value.trim() || "https://api.typesafe.ai";
  if (!(await ensureHostPermission(apiBase))) return status($("saveStatus"), "Permission refused.", false);
  const allowlist = $("allowlist").value
    .split(/[\s,]+/)
    .map((h) => h.replace(/^@/, "").toLowerCase())
    .filter(Boolean);
  await saveSettings({
    allowlist,
    model: $("model").value.trim() || "jev-1.13.0",
    minChars: Math.max(0, parseInt($("minChars").value, 10) || 0),
    apiBase,
    maxConcurrent: Math.min(20, Math.max(1, parseInt($("maxConcurrent").value, 10) || 6)),
  });
  status($("saveStatus"), "Saved.", true);
};

$("clearCache").onclick = async () => {
  await chrome.runtime.sendMessage({ type: "clear-cache" });
  status($("saveStatus"), "Cache cleared.", true);
};

// Local mock servers (http://localhost) need an explicit grant.
async function ensureHostPermission(base) {
  if (!base || base.startsWith("https://api.typesafe.ai")) return true;
  try {
    const origin = new URL(base).origin + "/*";
    return await chrome.permissions.request({ origins: [origin] });
  } catch {
    return false;
  }
}

render();
