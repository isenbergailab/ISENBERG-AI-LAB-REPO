// Runs on x.com. Finds posts, asks the background worker for a Jev verdict,
// then collapses, hides or labels them. All decisions come from the background.
(() => {
  const SEL_POST = 'article[data-testid="tweet"]';
  const pct = (p) => Math.round(p * 100) + "%";
  let alive = true;

  function textOf(el) {
    // innerText drops emoji <img alt>, which matters for slop detection
    let out = "";
    const walk = (n) => {
      if (n.nodeType === 3) out += n.nodeValue;
      else if (n.nodeName === "IMG") out += n.alt || "";
      else if (n.nodeName === "BR") out += "\n";
      else n.childNodes.forEach(walk);
    };
    walk(el);
    return out.trim();
  }

  function readPost(article) {
    const timeLink = article.querySelector('a[href*="/status/"] time')?.closest("a");
    const m = timeLink?.getAttribute("href")?.match(/^\/([^/]+)\/status\/(\d+)/);
    if (!m) return null;
    const texts = [...article.querySelectorAll('[data-testid="tweetText"]')];
    const inQuote = (el) => !!el.closest('div[role="link"]') && el.closest('div[role="link"]') !== article;
    const main = texts.find((t) => !inQuote(t));
    const quoted = texts.find((t) => inQuote(t));
    return {
      id: m[2],
      handle: m[1],
      text: main ? textOf(main) : "",
      quoted: quoted ? textOf(quoted) : "",
      isReply: /Replying to/.test(article.innerText.slice(0, 300)),
      hasMedia: !!article.querySelector('[data-testid="tweetPhoto"], [data-testid="videoPlayer"]'),
    };
  }

  function focalId() {
    return location.pathname.match(/\/status\/(\d+)/)?.[1];
  }

  function clear(article) {
    article.classList.remove("jevx-collapsed", "jevx-labelled");
    article.closest('[data-testid="cellInnerDiv"]')?.classList.remove("jevx-hidden");
    article.querySelectorAll(":scope > .jevx-bar").forEach((b) => b.remove());
  }

  function makeBar(article, verdict, post, revealed) {
    const bar = document.createElement("div");
    bar.className = "jevx-bar";
    const reasons = verdict.flags
      .map((id) => `${labelFor(id)} ${pct(verdict.probs[id])}`)
      .join(" · ");
    const info = document.createElement("span");
    info.className = "jevx-info";
    info.textContent = revealed ? reasons : `Filtered: ${reasons} · @${post.handle}`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "jevx-btn";
    btn.textContent = revealed ? "Collapse" : "Show";
    bar.append(info, btn);
    // keep clicks on the bar from opening the post (X navigates on article click)
    bar.addEventListener("click", (e) => e.stopPropagation());
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      article.dataset.jevxRevealed = revealed ? "" : "1";
      apply(article, verdict, post);
    });
    return bar;
  }

  const LABELS = { ragebait: "Ragebait", slop: "AI slop", bait: "Engagement bait" };
  const labelFor = (id) => LABELS[id] || id;

  function apply(article, verdict, post) {
    clear(article);
    if (!verdict || !verdict.top) return;
    const revealed = article.dataset.jevxRevealed === "1";
    if (verdict.mode === "hide" && !revealed) {
      (article.closest('[data-testid="cellInnerDiv"]') || article).classList.add("jevx-hidden");
      return;
    }
    if (verdict.mode === "label" || revealed) {
      article.classList.add("jevx-labelled");
      article.prepend(makeBar(article, verdict, post, true));
      return;
    }
    article.classList.add("jevx-collapsed");
    article.prepend(makeBar(article, verdict, post, false));
  }

  async function process(article, recheck = false) {
    const post = readPost(article);
    if (!post) return;
    article.dataset.jevxId = post.id;
    if (post.id === focalId()) { clear(article); return; } // never filter a post you opened on purpose
    if (!recheck) article.dataset.jevxRevealed = "";
    let verdict;
    try {
      verdict = await chrome.runtime.sendMessage({ type: "classify", post: { ...post, recheck } });
    } catch {
      alive = false; // extension reloaded; this old script is orphaned
      return;
    }
    // X recycles nodes; drop the answer if this article now shows a different post
    if (article.dataset.jevxId !== post.id) return;
    if (verdict?.error && verdict.error !== "no-key") console.debug("[jev-filter]", verdict.error);
    apply(article, verdict, post);
  }

  function scan() {
    if (!alive) return;
    for (const article of document.querySelectorAll(SEL_POST)) {
      const id = readPost(article)?.id;
      if (!id) continue;
      if (article.dataset.jevxId === id) continue;
      if (article.dataset.jevxId) clear(article); // recycled node
      process(article);
    }
  }

  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; scan(); });
  }).observe(document.body, { childList: true, subtree: true });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "sync" || !changes.settings) return;
    // thresholds or mode changed: re-evaluate from cache, no new API calls
    for (const article of document.querySelectorAll(SEL_POST + "[data-jevx-id]")) process(article, true);
  });

  scan();
})();
