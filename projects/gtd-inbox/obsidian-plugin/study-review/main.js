"use strict";
const {Plugin, MarkdownView, Modal, Notice, Setting} = require("obsidian");
const gre = (() => {
const module = {exports:{}};
"use strict";

const META = /^<!-- study-review:(\{[^\r\n]*\}) -->$/m;
const ENTRY = /^## Q(\d+)\b[^\r\n]*$/gm;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const QUEUE_BEGIN = "<!-- study-review:queue-begin -->";
const QUEUE_END = "<!-- study-review:queue-end -->";
const REVIEWED_BEGIN = "<!-- study-review:reviewed-begin -->";
const REVIEWED_END = "<!-- study-review:reviewed-end -->";

function addDays(day, count) {
  const date = new Date(`${day}T12:00:00Z`);
  if (!DATE.test(day) || Number.isNaN(date.getTime())) throw new Error("Invalid date");
  date.setUTCDate(date.getUTCDate() + count);
  return date.toISOString().slice(0, 10);
}

function field(raw, name) {
  return raw.match(new RegExp(`^- ${name}:\\s*(.*)$`, "m"))?.[1]?.trim() || "";
}

function sections(note) {
  const heads = [...note.matchAll(ENTRY)];
  return heads.map((head, index) => ({
    q: Number(head[1]), start: head.index,
    end: index + 1 < heads.length ? heads[index + 1].index : note.length,
    raw: note.slice(head.index, index + 1 < heads.length ? heads[index + 1].index : note.length),
  }));
}

function parseEntries(note) {
  return sections(note).map(section => {
    const found = section.raw.match(META);
    if (!found) return null;
    let meta;
    try {meta = JSON.parse(found[1]);} catch {return null;}
    if (!meta.id) return null;
    return {...section, id: meta.id, ready: meta.ready || "", streak: meta.streak || 0,
      attempts: Array.isArray(meta.attempts) ? meta.attempts : [],
      slots: (field(section.raw, "Retest").match(/\d{4}-\d{2}-\d{2}/g) || []).sort(),
      question: field(section.raw, "Question"), mine: field(section.raw, "Mine"),
      right: field(section.raw, "Right"), explanation: field(section.raw, "Explanation"),
      why: field(section.raw, "Why")};
  }).filter(Boolean);
}

function replaceEntry(note, entry, raw) {
  return note.slice(0, entry.start) + raw.trimEnd() + "\n\n" + note.slice(entry.end).replace(/^\n*/, "");
}

function removeEntry(note, id) {
  const entry = parseEntries(note).find(x => x.id === id);
  if (!entry) throw new Error("Question missing");
  return (note.slice(0, entry.start).trimEnd() + "\n\n" + note.slice(entry.end).trimStart()).trim() + "\n";
}

function appendEntry(note, raw) {
  const entry = parseEntries(raw)[0];
  if (!entry || parseEntries(note).some(x => x.id === entry.id)) throw new Error("Duplicate question");
  return note.trimEnd() + "\n\n" + raw.trim() + "\n";
}

function revise(entry, meta, slots) {
  let raw = entry.raw.replace(META, `<!-- study-review:${JSON.stringify(meta)} -->`);
  raw = raw.replace(/^- Retest:.*$/m, `- Retest: ${slots.join(", ")}`);
  const history = meta.attempts.map(a =>
    `  - ${a.at.slice(0, 10)} · ${a.result === "correct" ? "Correct" : "Wrong"} · scheduled ${a.slot}`).join("\n");
  raw = raw.replace(/^- Attempts:.*(?:\r?\n[ \t]+-.*)*/m, `- Attempts:${history ? "\n" + history : ""}`);
  return raw;
}

function addTemplate(note, day, id, nextNumber=0) {
  if (!DATE.test(day) || !/^gre-[a-z0-9-]+$/i.test(id)) throw new Error("Invalid entry");
  const q = Math.max(Math.max(0, ...sections(note).map(x => x.q)) + 1, nextNumber);
  const meta = {id, ready:"", streak:0, attempts:[]};
  const raw = `## Q${q} · ${day}\n<!-- study-review:${JSON.stringify(meta)} -->\n` +
    "- Question: ![[]]\n- Mine: \n- Right: \n- Explanation: ![[]]\n" +
    "- Why: \n- Source: \n- Clue: \n- Rule: \n- Tags: \n- Retest: \n- Attempts:\n";
  const front = note.match(/^---\r?\n[\s\S]*?\r?\n---\r?\n/);
  return front ? front[0] + raw + "\n" + note.slice(front[0].length).replace(/^\n*/, "") :
    raw + "\n" + note.replace(/^\n*/, "");
}

function ready(note, id, day) {
  const entry = parseEntries(note).find(x => x.id === id);
  if (!entry) throw new Error("Question missing");
  if (entry.ready) throw new Error("Question already ready");
  if (!/!\[\[[^\]]+\]\]/.test(entry.question)) throw new Error("Question image required");
  if (!entry.mine) throw new Error("Your answer required");
  if (!entry.right) throw new Error("Correct answer required");
  if (!/!\[\[[^\]]+\]\]/.test(entry.explanation)) throw new Error("Explanation image required");
  if (!entry.why) throw new Error("Why required");
  const meta = {id, ready:day, streak:0, attempts:[]};
  return replaceEntry(note, entry, revise(entry, meta, [addDays(day, 3), addDays(day, 7)]));
}

function setDates(note, id, slots) {
  const entry = parseEntries(note).find(x => x.id === id);
  if (!entry || !entry.ready) throw new Error("Ready question missing");
  if (!slots.length || slots.some(x => !DATE.test(x) || Number.isNaN(Date.parse(x))))
    throw new Error("Enter review dates as YYYY-MM-DD");
  const meta = {id, ready:entry.ready, streak:entry.streak, attempts:entry.attempts};
  return replaceEntry(note, entry, revise(entry, meta, [...slots].sort()));
}

function grade(note, id, result, at) {
  if (!/^(correct|wrong)$/.test(result)) throw new Error("Choose Correct or Wrong");
  const entry = parseEntries(note).find(x => x.id === id);
  if (!entry || !entry.ready) throw new Error("Ready question missing");
  const day = at.slice(0, 10);
  if (!DATE.test(day)) throw new Error("Invalid review date");
  if (entry.attempts.some(a => a.at.slice(0, 10) === day)) throw new Error("Grade once per day");
  const slot = entry.slots.find(x => x <= day);
  if (!slot) throw new Error("No review due yet");
  const beforeSlots = [...entry.slots];
  const slots = [...entry.slots];
  slots.splice(slots.indexOf(slot), 1);
  const streak = result === "correct" ? entry.streak + 1 : 0;
  if (result === "wrong") slots.push(addDays(day, 3));
  else if (streak === 1 && !slots.length) slots.push(addDays(day, 7));
  const archive = streak >= 2;
  const attempts = [...entry.attempts,
    {at, result, slot, beforeSlots, beforeStreak:entry.streak}];
  const meta = {id, ready:entry.ready, streak, attempts};
  return {note:replaceEntry(note, entry, revise(entry, meta, archive ? [] : slots.sort())), archive};
}

function undo(note, id) {
  const entry = parseEntries(note).find(x => x.id === id);
  if (!entry || !entry.attempts.length) throw new Error("No grade to undo");
  const attempts = entry.attempts.slice(0, -1);
  const last = entry.attempts.at(-1);
  const meta = {id, ready:entry.ready, streak:last.beforeStreak, attempts};
  return replaceEntry(note, entry, revise(entry, meta, last.beforeSlots));
}

function renderQueue(entries, day) {
  const ordered = entries.filter(x => x.ready && x.slots.length)
    .sort((a,b) => a.slots[0].localeCompare(b.slots[0]) || a.q-b.q);
  const card = e => `### Q${e.q} · ${e.slots[0] <= day ? "Due" : "Upcoming"} ${e.slots[0]}\n` +
    `${e.question}\n\nRetest: ${e.slots.join(", ")} · Correct streak: ${e.streak}\n` +
    `<!-- study-review:card=${e.id} -->\n`;
  const due = ordered.filter(x => x.slots[0] <= day);
  const later = ordered.filter(x => x.slots[0] > day);
  return `## Due\n\n${due.length ? due.map(card).join("\n") : "Nothing due.\n"}\n` +
    `## Upcoming\n\n${later.length ? later.map(card).join("\n") : "Nothing upcoming.\n"}`;
}

function reviewedCard(entry, result, day) {
  return `### Q${entry.q} · ${result === "correct" ? "Correct" : "Wrong"} · ${day}\n` +
    `${entry.question}\n\nMine: ${entry.mine}\n\nRight: ${entry.right}\n\n` +
    `${entry.explanation}\n\nWhy: ${entry.why}\n\n` +
    `<!-- study-review:reviewed-card=${entry.id} -->\n`;
}

function makePractice(queue, reviewed="") {
  return `---\ntype: reference\nproject: ["[[STUDY_STUDYING]]"]\n---\n# STUDY Question Review Practice\n\n` +
    "Review due questions here. Grade once daily. The explanation appears after grading.\n\n" +
    `${QUEUE_BEGIN}\n${queue.trimEnd()}\n${QUEUE_END}\n\n` +
    `${REVIEWED_BEGIN}\n## Reviewed today\n\n${reviewed.trimEnd()}\n${REVIEWED_END}\n`;
}

function updateQueue(practice, queue) {
  const start = practice.indexOf(QUEUE_BEGIN);
  const end = practice.indexOf(QUEUE_END);
  if (start < 0 || end < start) throw new Error("Practice markers missing");
  return practice.slice(0, start + QUEUE_BEGIN.length) + "\n" + queue.trimEnd() + "\n" + practice.slice(end);
}

function appendReviewed(practice, card) {
  const end = practice.indexOf(REVIEWED_END);
  if (end < 0) throw new Error("Reviewed marker missing");
  return practice.slice(0, end).trimEnd() + "\n\n" + card.trimEnd() + "\n" + practice.slice(end);
}

function clearReviewed(practice) {
  const start = practice.indexOf(REVIEWED_BEGIN);
  const end = practice.indexOf(REVIEWED_END);
  if (start < 0 || end < start) throw new Error("Reviewed markers missing");
  return practice.slice(0, start + REVIEWED_BEGIN.length) +
    "\n## Reviewed today\n\n" + practice.slice(end);
}

function removeReviewedCard(practice, id) {
  const marker = `<!-- study-review:reviewed-card=${id} -->`;
  const reviewedStart = practice.indexOf(REVIEWED_BEGIN);
  const end = practice.lastIndexOf(marker);
  if (reviewedStart < 0 || end < reviewedStart) return practice;
  const start = practice.lastIndexOf("\n### Q", end);
  if (start < reviewedStart) return practice;
  let after = end + marker.length;
  while (practice[after] === "\n") after++;
  return practice.slice(0, start).trimEnd() + "\n\n" + practice.slice(after);
}

function shouldClearReviewed(practice, modified, now) {
  return now - modified >= 10 * 60 * 1000 && practice.includes("study-review:reviewed-card=");
}

module.exports = {addDays, addTemplate, appendEntry, appendReviewed, clearReviewed, grade,
  makePractice, parseEntries, ready, removeEntry, removeReviewedCard, renderQueue,
  reviewedCard, setDates, shouldClearReviewed, undo, updateQueue};

return module.exports;
})();

const ROOT = "04_REFERENCE/STUDY/";
const SOURCE = ROOT + "INCORRECT_ANSWERS_REVIEW.md";
const PRACTICE = ROOT + "STUDY_QUESTION_REVIEW_PRACTICE.md";
const ARCHIVE = ROOT + "STUDY_QUESTION_REVIEW_ARCHIVE.md";

function nowInNewYork() {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone:"America/New_York", year:"numeric", month:"2-digit", day:"2-digit",
    hour:"2-digit", minute:"2-digit", second:"2-digit", hourCycle:"h23",
  }).formatToParts(new Date()).filter(x => x.type !== "literal").map(x => [x.type, x.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`;
}

class DatesModal extends Modal {
  constructor(plugin, id, dates) {super(plugin.app); this.plugin=plugin; this.id=id; this.dates=dates;}
  onOpen() {
    this.titleEl.setText("Change STUDY review dates");
    let value = this.dates.join(", ");
    new Setting(this.contentEl).setName("Dates (YYYY-MM-DD)")
      .addText(text => text.setValue(value).onChange(next => {value=next;}));
    new Setting(this.contentEl).addButton(button => button.setButtonText("Save dates").setCta()
      .onClick(async () => {
        button.setDisabled(true);
        try {await this.plugin.changeDates(this.id, value.split(",").map(x => x.trim())); this.close();}
        catch (error) {new Notice(error.message || String(error)); button.setDisabled(false);}
      }));
  }
}

module.exports = class STUDYReview extends Plugin {
  async onload() {
    this.observer = new MutationObserver(() => this.scheduleDecorate());
    this.observer.observe(document.body, {subtree:true, childList:true});
    this.register(() => this.observer.disconnect());
    this.registerEvent(this.app.workspace.on("active-leaf-change", () => this.scheduleDecorate()));
    this.registerEvent(this.app.workspace.on("layout-change", () => this.scheduleDecorate()));
    this.registerEvent(this.app.vault.on("modify", file => {
      if ((file.path === SOURCE || file.path === ARCHIVE) && !this.busy) this.scheduleRefresh();
    }));
    this.currentDay = nowInNewYork().slice(0,10);
    this.registerInterval(window.setInterval(() => {
      this.clearIdle().catch(this.report);
      const day = nowInNewYork().slice(0,10);
      if (day !== this.currentDay) {this.currentDay=day; this.refresh().catch(this.report);}
    }, 30000));
    try {await this.initialize();} catch (error) {this.report(error);}
    this.scheduleDecorate();
  }

  report = error => new Notice(`STUDY review: ${error.message || error}`);
  file(path) {
    const file = this.app.vault.getAbstractFileByPath(path);
    if (!file) throw new Error(`Missing ${path.split("/").pop()}`);
    return file;
  }
  async initialize() {
    if (!this.app.vault.getAbstractFileByPath(SOURCE)) throw new Error("Incorrect answers note missing");
    if (!this.app.vault.getAbstractFileByPath(ARCHIVE))
      await this.app.vault.create(ARCHIVE, "---\ntype: reference\nproject: [\"[[STUDY_STUDYING]]\"]\n---\n# STUDY Question Review Archive\n\nQuestions move here after two consecutive correct reviews.\n");
    if (!this.app.vault.getAbstractFileByPath(PRACTICE))
      await this.app.vault.create(PRACTICE, gre.makePractice(gre.renderQueue([], nowInNewYork().slice(0,10))));
    await this.clearIdle();
    await this.refresh();
  }
  scheduleRefresh() {
    if (this.refreshTimer) window.clearTimeout(this.refreshTimer);
    this.refreshTimer = window.setTimeout(() => {
      this.refreshTimer=null; this.refresh().catch(this.report);
    }, 450);
  }
  async entries() {
    const [source, archive] = await Promise.all([
      this.app.vault.read(this.file(SOURCE)), this.app.vault.read(this.file(ARCHIVE))]);
    const archived = new Set(gre.parseEntries(archive).map(x => x.id));
    return gre.parseEntries(source).filter(x => !archived.has(x.id));
  }
  async refresh() {
    if (this.busy) return;
    const queue = gre.renderQueue(await this.entries(), nowInNewYork().slice(0,10));
    const file = this.file(PRACTICE);
    const before = await this.app.vault.read(file);
    const after = gre.updateQueue(before, queue);
    if (after !== before) await this.app.vault.process(file, current => gre.updateQueue(current, queue));
    this.scheduleDecorate();
  }
  async clearIdle() {
    const file = this.app.vault.getAbstractFileByPath(PRACTICE);
    if (!file || this.busy) return;
    const before = await this.app.vault.read(file);
    if (!gre.shouldClearReviewed(before, file.stat.mtime, Date.now())) return;
    await this.app.vault.process(file, current => gre.clearReviewed(current));
  }

  scheduleDecorate() {
    if (this.decorateTimer) return;
    this.decorateTimer = window.setTimeout(() => {this.decorateTimer=null; this.decorate();}, 100);
  }
  decorate() {
    this.app.workspace.iterateAllLeaves(leaf => {
      const view = leaf.view;
      if (!(view instanceof MarkdownView) || !view.file) return;
      const container = view.containerEl;
      const path = view.file.path;
      const target = path === SOURCE || path === PRACTICE;
      for (const old of container.querySelectorAll(":scope > .study-review-toolbar"))
        if (old.dataset.path !== path || !target) old.remove();
      if (!target) return;
      for (const line of container.querySelectorAll(".markdown-source-view .cm-line")) {
        line.classList.toggle("study-review-marker", /^<!-- study-review:.* -->$/.test(line.textContent.trim()));
      }
      if (!container.querySelector(":scope > .study-review-toolbar")) {
        const toolbar = document.createElement("div");
        toolbar.className = "study-review-toolbar";
        toolbar.dataset.path = path;
        if (path === SOURCE) {
          this.button(toolbar, "Add question", () => this.addQuestion(view));
          this.button(toolbar, "Ready newest draft", () => this.readyNewest(view));
        } else {
          this.button(toolbar, "Undo last grade", () => this.undoLast());
          this.button(toolbar, "Refresh", () => this.refresh());
        }
        container.prepend(toolbar);
      }
      if (path !== PRACTICE) return;
      for (const line of container.querySelectorAll(".markdown-source-view .cm-line, .markdown-preview-view h3")) {
        if (line.querySelector(".study-review-controls")) continue;
        const match = line.textContent.trim().match(/^(?:###\s*)?Q(\d+)\s*·\s*(Due|Upcoming)\s+\d{4}-\d{2}-\d{2}/);
        if (!match) continue;
        const q = Number(match[1]);
        const controls = document.createElement("span");
        controls.className = "study-review-controls";
        if (match[2] === "Due") {
          this.button(controls, "Correct", () => this.gradeQuestion(q, "correct"));
          this.button(controls, "Wrong", () => this.gradeQuestion(q, "wrong"));
        }
        this.button(controls, "Edit dates", () => this.editDates(q));
        line.appendChild(controls);
      }
    });
  }
  button(parent, label, action) {
    const button = document.createElement("button");
    button.type="button"; button.textContent=label;
    button.addEventListener("click", async event => {
      event.preventDefault(); event.stopPropagation();
      button.disabled=true;
      try {await action();} catch (error) {this.report(error);} finally {button.disabled=false;}
    });
    parent.appendChild(button);
  }
  async saveView(view) {if (typeof view.save === "function") await view.save();}
  async addQuestion(view) {
    await this.saveView(view);
    const stamp=nowInNewYork(), id=`gre-${Date.now().toString(36)}-${Math.random().toString(36).slice(2,8)}`;
    const archive=gre.parseEntries(await this.app.vault.read(this.file(ARCHIVE)));
    const next=Math.max(0, ...archive.map(x => x.q)) + 1;
    await this.app.vault.process(this.file(SOURCE), current => gre.addTemplate(current, stamp.slice(0,10), id, next));
    new Notice("Question template added at top.");
  }
  async readyNewest(view) {
    await this.saveView(view);
    const file=this.file(SOURCE), before=await this.app.vault.read(file);
    const draft=gre.parseEntries(before).find(x => !x.ready);
    if (!draft) throw new Error("No unfinished question found");
    await this.app.vault.process(file, current => gre.ready(current, draft.id, nowInNewYork().slice(0,10)));
    await this.refresh();
    new Notice(`Q${draft.q} ready. Reviews begin in three days.`);
  }
  async changeDates(id, dates) {
    await this.app.vault.process(this.file(SOURCE), current => gre.setDates(current, id, dates));
    await this.refresh();
  }
  async editDates(q) {
    const entry=(await this.entries()).find(x => x.q === q);
    if (!entry) throw new Error("Question moved");
    new DatesModal(this, entry.id, entry.slots).open();
  }
  showReviewed(q, result) {
    if (typeof window === "undefined") return;
    const label = `### Q${q} · ${result === "correct" ? "Correct" : "Wrong"}`;
    let tries = 0;
    const seek = () => {
      let shown = false;
      this.app.workspace.iterateAllLeaves(leaf => {
        const view = leaf.view;
        if (!(view instanceof MarkdownView) || view.file?.path !== PRACTICE) return;
        const editor = view.editor;
        if (editor?.getValue) {
          const lines = editor.getValue().split(/\r?\n/);
          const line = lines.findLastIndex(text => text.startsWith(label));
          if (line >= 0) {
            const pos = {line, ch:0};
            editor.setCursor(pos);
            editor.scrollIntoView({from:pos, to:pos}, true);
            shown = true;
          }
        }
        if (!shown) {
          const headings = [...view.containerEl.querySelectorAll(".markdown-preview-view h3")];
          const heading = headings.filter(el => el.textContent.startsWith(label.slice(4))).at(-1);
          if (heading) {heading.scrollIntoView({block:"center"}); shown=true;}
        }
      });
      if (!shown && tries++ < 5) window.setTimeout(seek, 250);
    };
    window.setTimeout(seek, 200);
  }
  async gradeQuestion(q, result) {
    if (this.busy) throw new Error("Another review change is still saving");
    this.busy=true;
    try {
      const sourceFile=this.file(SOURCE), source=await this.app.vault.read(sourceFile);
      const entry=gre.parseEntries(source).find(x => x.q === q);
      if (!entry) throw new Error("Question moved");
      const at=nowInNewYork(), outcome=gre.grade(source, entry.id, result, at);
      if (outcome.archive) {
        const archived=gre.parseEntries(outcome.note).find(x => x.id === entry.id);
        await this.app.vault.process(this.file(ARCHIVE), current => gre.appendEntry(current, archived.raw));
        try {
          await this.app.vault.process(sourceFile, current => {
            const live=gre.parseEntries(current).find(x => x.id === entry.id);
            if (!live || live.raw !== entry.raw) throw new Error("Question changed during archive");
            return gre.removeEntry(current, entry.id);
          });
        } catch (error) {
          await this.app.vault.process(this.file(ARCHIVE), current => gre.removeEntry(current, entry.id));
          throw error;
        }
      } else {
        await this.app.vault.process(sourceFile, current => gre.grade(current, entry.id, result, at).note);
      }
      const saved=await this.app.vault.read(this.file(outcome.archive ? ARCHIVE : SOURCE));
      const updated=gre.parseEntries(saved).find(x => x.id === entry.id);
      const queue=gre.renderQueue(await this.entries(), at.slice(0,10));
      await this.app.vault.process(this.file(PRACTICE), current =>
        gre.appendReviewed(gre.updateQueue(current, queue), gre.reviewedCard(updated, result, at.slice(0,10))));
      this.showReviewed(q, result);
      new Notice(result === "correct" ? "Correct recorded. Explanation shown below." :
        "Wrong recorded. Explanation shown below.");
    } finally {this.busy=false; this.scheduleDecorate();}
  }
  async undoLast() {
    if (this.busy) throw new Error("Another review change is still saving");
    this.busy=true;
    try {
      const [sourceText, archiveText]=await Promise.all([
        this.app.vault.read(this.file(SOURCE)), this.app.vault.read(this.file(ARCHIVE))]);
      const all=[...gre.parseEntries(sourceText).map(x => ({...x,path:SOURCE})),
        ...gre.parseEntries(archiveText).map(x => ({...x,path:ARCHIVE}))];
      const last=all.filter(x => x.attempts.length)
        .sort((a,b) => b.attempts.at(-1).at.localeCompare(a.attempts.at(-1).at))[0];
      if (!last) throw new Error("No grade to undo");
      if (last.path === ARCHIVE) {
        const restored=gre.parseEntries(gre.undo(archiveText, last.id)).find(x => x.id === last.id);
        await this.app.vault.process(this.file(SOURCE), current => gre.appendEntry(current, restored.raw));
        await this.app.vault.process(this.file(ARCHIVE), current => gre.removeEntry(current, last.id));
      } else {
        await this.app.vault.process(this.file(SOURCE), current => gre.undo(current, last.id));
      }
      const queue=gre.renderQueue(await this.entries(), nowInNewYork().slice(0,10));
      await this.app.vault.process(this.file(PRACTICE), current =>
        gre.removeReviewedCard(gre.updateQueue(current, queue), last.id));
      new Notice(`Undid Q${last.q}'s last grade.`);
    } finally {this.busy=false; this.scheduleDecorate();}
  }
};
