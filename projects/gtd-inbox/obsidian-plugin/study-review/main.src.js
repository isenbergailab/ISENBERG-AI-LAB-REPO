"use strict";
const {Plugin, MarkdownView, Modal, Notice, Setting} = require("obsidian");
const gre = require("./core.js");

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
