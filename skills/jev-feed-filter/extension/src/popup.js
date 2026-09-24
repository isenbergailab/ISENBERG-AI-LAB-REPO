import { loadSettings, saveSettings, getApiKey } from "./settings.js";

const $ = (id) => document.getElementById(id);

async function refresh() {
  const s = await loadSettings();
  $("enabled").checked = s.enabled;
  const { stats, keyError, cost } = await chrome.runtime.sendMessage({ type: "stats" });
  $("checked").textContent = stats.checked + stats.cacheHits;
  for (const c of ["ragebait", "slop", "bait"]) $("f-" + c).textContent = stats.filtered[c] || 0;
  $("calls").textContent = `${stats.calls} / ${stats.cacheHits}`;
  $("cost").textContent = "$" + cost.toFixed(5);
  const key = await getApiKey();
  $("warn").textContent = !key
    ? "No API key yet. Add one in Settings."
    : keyError
      ? keyError + ". Check the key in Settings."
      : stats.errors
        ? `${stats.errors} request errors this session.`
        : "";
}

$("enabled").onchange = (e) => saveSettings({ enabled: e.target.checked });
$("settings").onclick = () => chrome.runtime.openOptionsPage();

refresh();
setInterval(refresh, 1500);
