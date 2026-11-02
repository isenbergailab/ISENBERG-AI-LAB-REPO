"use strict";
const {Plugin, MarkdownView, Modal, Notice, Setting} = require("obsidian");
const TASK_VIEWS = new Set(["_agent/TODAY.md", "01_GTD/NEXT_ACTIONS.md", "01_GTD/WAITING_FOR.md"]);
const INBOX = "00_INBOX/INBOX.md";
const MARKER = /^<!-- gtd-agent:(?:begin|end|proposal id=[a-f0-9]{12}|item-end|feedback-begin|feedback-end) -->$/;

function taskDetails(line) {
  const id = line.match(/(?:🆔\s*([^\s]+)|\^(gtd-[A-Za-z0-9-]+))/u);
  const created = line.match(/➕\s*(\d{4}-\d{2}-\d{2})/u);
  const due = line.match(/📅\s*(\d{4}-\d{2}-\d{2})/u);
  const tags = [...new Set(line.match(/#[A-Za-z][\w/-]*/g) || [])];
  return [id && `ID: ${id[1] || id[2]}`, created && `Added: ${created[1]}`,
    due && `Due: ${due[1]}`, tags.length && `Tags: ${tags.join(" ")}`].filter(Boolean).join(" · ") || "No extra details";
}

function taskLabel(text) {
  return text.replace(/^\s*[-*+]\s+\[[^\]]\]\s+/u, "")
    .replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/gu, "$2")
    .replace(/\[\[([^\]]+)\]\]/gu, (_, path) => path.split("/").pop())
    .replace(/\[([^\]]+)\]\([^)]*\)/gu, "$1")
    .replace(/\s+(?:📅|➕|🆔|🛫|⏳|✅|❌|🔁|⛔|🔺|🔼|🔽).*$/u, "")
    .replace(/\s+\^[A-Za-z0-9-]+\s*$/u, "")
    .replace(/#[A-Za-z][\w/-]*/gu, "")
    .replace(/[*_`]/gu, "")
    .replace(/\s+/gu, " ").trim();
}

function matchingTaskLine(contents, displayed, status) {
  const expected = taskLabel(displayed);
  if (!expected) return null;
  const matches = contents.split(/\r?\n/u).flatMap((line, index) => {
    const checkbox = line.match(/^\s*[-*+]\s+\[([^\]])\]\s+/u);
    return checkbox && checkbox[1] === status && taskLabel(line) === expected ? [{line, index}] : [];
  });
  return matches.length === 1 ? matches[0] : null;
}

function projectKey(path, line, app) {
  const byPath = path.match(/^02_PROJECTS\/([^/]+)\//);
  if (byPath) return byPath[1];
  for (const match of line.matchAll(/\[\[([^\]#|]+)(?:[|#][^\]]*)?\]\]/g)) {
    const file = app.metadataCache.getFirstLinkpathDest(match[1], path);
    const front = file && app.metadataCache.getFileCache(file)?.frontmatter;
    if (front?.type === "project") return front.key || file.basename;
  }
  return "";
}

function privateTask(file, line, project, app) {
  if (/#private\b/i.test(line) || app.metadataCache.getFileCache(file)?.frontmatter?.private === true) return true;
  const targets = [...line.matchAll(/\[\[([^\]#|]+)(?:[|#][^\]]*)?\]\]/g)].map(match => match[1]);
  if (project) targets.push(project);
  for (const key of targets) {
    const target = app.metadataCache.getFirstLinkpathDest(key, file.path);
    const front = target && app.metadataCache.getFileCache(target)?.frontmatter;
    if (front?.private === true) return true;
    const area = typeof front?.area === "string" && front.area.match(/\[\[([^\]]+)\]\]/)?.[1];
    const areaFile = area && app.metadataCache.getFirstLinkpathDest(area, target.path);
    if (areaFile && app.metadataCache.getFileCache(areaFile)?.frontmatter?.private === true) return true;
  }
  return false;
}

function captureLine(text, file, line, project, isPrivate) {
  const compact = text.replace(/\s+/g, " ").trim();
  const id = line.match(/\^(gtd-[A-Za-z0-9-]+)/);
  const source = file.path.replace(/\.md$/i, "") + (id ? `#^${id[1]}` : "");
  return `Quick update: ${compact}${project ? ` [[${project}]]` : ""} ` +
    `(source: [[${source}|task]])${isPrivate ? " #private" : ""}`;
}

class UpdateModal extends Modal {
  constructor(plugin, row, task) { super(plugin.app); this.plugin = plugin; this.row = row; this.task = task; }
  onOpen() {
    this.titleEl.setText("Quick update");
    const input = this.contentEl.createEl("textarea");
    input.rows = 4;
    input.placeholder = "What changed or became clear?";
    new Setting(this.contentEl).addButton(button => button.setButtonText("Save update").setCta().onClick(async () => {
      button.setDisabled(true);
      const message = input.value.replace(/\s+/g, " ").trim();
      if (!message || message.length > 1500 || /<!--|-->/u.test(message)) {
        new Notice("Write one update, up to 1500 characters."); button.setDisabled(false); return;
      }
      try {
        const source = await this.plugin.resolveTask(this.row);
        if (!source || source.file.path !== this.task.file.path || source.line !== this.task.line) {
          new Notice("Task changed. Reopen Update."); return;
        }
        const project = projectKey(source.file.path, source.line, this.app);
        const line = captureLine(message, source.file, source.line, project,
          privateTask(source.file, source.line, project, this.app));
        const inbox = this.app.vault.getAbstractFileByPath(INBOX);
        if (!inbox) throw new Error("Inbox missing");
        const entry = `- ${line}\n`;
        const count = text => text.split(entry).length - 1;
        let wanted = 0;
        const append = () => this.app.vault.process(inbox, current => {
          wanted = count(current) + 1;
          return current + (current.endsWith("\n") ? "" : "\n") + entry;
        });
        await append();
        for (const delay of [500, 1200]) {
          await new Promise(resolve => window.setTimeout(resolve, delay));
          if (count(await this.app.vault.read(inbox)) < wanted) await append();
        }
        if (count(await this.app.vault.read(inbox)) < wanted) throw new Error("Inbox write needs retry");
        this.contentEl.empty();
        this.contentEl.createEl("p", {text: "Update saved to Inbox."});
        new Setting(this.contentEl)
          .addButton(done => done.setButtonText("Complete task").onClick(() => {
            const checkbox = this.row.querySelector("input[type='checkbox']");
            if (checkbox && this.row.isConnected) checkbox.click();
            else new Notice("Task moved. Complete it in task view.");
            this.close();
          }))
          .addButton(later => later.setButtonText("Leave open").onClick(() => this.close()));
      } catch (error) { new Notice(`Update failed: ${error.message || error}`); button.setDisabled(false); }
    }));
    input.focus();
  }
}

module.exports = class GTDQuickUpdate extends Plugin {
  async onload() {
    this.observer = new MutationObserver(() => this.schedule());
    this.observer.observe(document.body, {subtree:true, childList:true, characterData:true});
    this.register(() => this.observer.disconnect());
    this.registerEvent(this.app.workspace.on("active-leaf-change", () => this.schedule()));
    this.registerEvent(this.app.workspace.on("layout-change", () => this.schedule()));
    this.schedule();
  }
  schedule() {
    if (this.timer) return;
    this.timer = window.setTimeout(() => {this.timer = null; this.decorate();}, 100);
  }
  decorate() {
    this.app.workspace.iterateAllLeaves(leaf => {
      const view = leaf.view;
      if (!(view instanceof MarkdownView) || !view.file) return;
      const container = view.containerEl;
      const taskView = TASK_VIEWS.has(view.file.path);
      container.classList.toggle("gtd-agent-task-view", taskView);
      container.classList.toggle("gtd-agent-approval-view", view.file.path === "_agent/APPROVAL.md");
      if (taskView) for (const row of container.querySelectorAll(".block-language-tasks li.task-list-item")) {
        this.decorateTask(row, view.file.path);
      }
      if (view.file.path === "_agent/APPROVAL.md") for (const line of container.querySelectorAll(".cm-line")) {
        line.classList.toggle("gtd-agent-marker", MARKER.test(line.textContent.trim()));
      }
    });
  }
  decorateTask(row, viewPath) {
    if (row.querySelector(":scope > .gtd-agent-controls")) return;
    if (!row.querySelector(".tasks-backlink a.internal-link") || !row.querySelector(".task-description")) return;
    row.dataset.gtdViewPath = viewPath;
    const controls = document.createElement("span");
    controls.className = "gtd-agent-controls";
    const details = document.createElement("button");
    details.type = "button"; details.textContent = "Details";
    details.addEventListener("click", async event => {
      event.preventDefault(); event.stopPropagation();
      const existing = row.querySelector(":scope > .gtd-agent-inline-details");
      if (existing) {existing.remove(); return;}
      const task = await this.resolveTask(row);
      if (!task) {new Notice("Task source unavailable."); return;}
      const panel = document.createElement("div");
      panel.className = "gtd-agent-inline-details";
      panel.textContent = taskDetails(task.line);
      row.appendChild(panel);
    });
    const update = document.createElement("button");
    update.type = "button"; update.textContent = "Update";
    update.addEventListener("click", async event => {
      event.preventDefault(); event.stopPropagation();
      const task = await this.resolveTask(row);
      if (!task) {new Notice("Task source unavailable."); return;}
      new UpdateModal(this, row, task).open();
    });
    controls.append(details, update);
    row.appendChild(controls);
  }
  async resolveTask(row) {
    const backlink = row.querySelector(".tasks-backlink a.internal-link");
    const displayed = row.querySelector(".task-description")?.textContent || "";
    const label = backlink?.textContent.trim() || "";
    const context = row.dataset.gtdViewPath || "";
    const candidates = [label];
    if (label.includes(" > ")) candidates.push(label.slice(0, label.lastIndexOf(" > ")));
    let file = null;
    for (const candidate of candidates) {
      file = this.app.metadataCache.getFirstLinkpathDest(candidate.replace(/^\//u, ""), context);
      if (file) break;
    }
    if (!file) return null;
    const match = matchingTaskLine(await this.app.vault.read(file), displayed, row.getAttribute("data-task") || " ");
    return match && {file, ...match};
  }
};
