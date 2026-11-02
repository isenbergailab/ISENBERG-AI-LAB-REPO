"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const code = fs.readFileSync(__dirname + "/main.js", "utf8") +
  "\nmodule.exports.helpers = {captureLine, projectKey, privateTask, taskDetails};";
const sandbox = {module: {exports: {}}, require(name) {
  if (name !== "obsidian") throw new Error(name);
  return {Plugin: class {}, MarkdownView: class {}, Modal: class {}, Notice: class {}, Setting: class {}};
}};
vm.runInNewContext(code, sandbox);
const {captureLine, projectKey, privateTask, taskDetails} = sandbox.module.exports.helpers;
const file = {path: "02_PROJECTS/TOPIC_EXAM_STUDY/TOPIC_EXAM_STUDY.md"};
const task = "- [ ] Review chapter #computer #next ➕ 2026-10-16 📅 2026-10-20 ^gtd-abc";
const app = {metadataCache: {getFileCache() {return {frontmatter: {private: false}};},
  getFirstLinkpathDest() {return null;}}};
assert.equal(projectKey(file.path, task, app), "TOPIC_EXAM_STUDY");
assert.match(taskDetails(task), /Added: 2026-10-16/);
assert.match(taskDetails(task), /Due: 2026-10-20/);
assert.equal(captureLine("Learned a new rule", file, task, "TOPIC_EXAM_STUDY", false),
  "Quick update: Learned a new rule [[TOPIC_EXAM_STUDY]] " +
  "(source: [[02_PROJECTS/TOPIC_EXAM_STUDY/TOPIC_EXAM_STUDY#^gtd-abc|task]])");
assert.equal(projectKey("01_GTD/SINGLE_ACTIONS.md", "- [ ] Buy milk #errands", app), "");
assert.equal(privateTask(file, task + " #private", "TOPIC_EXAM_STUDY", app), true);

const PluginClass = sandbox.module.exports;
const labFile = {path: "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"};
const labTask = "- [ ] Ask sample coordinator about fictional workshop booking #computer #next 📅 2026-10-14 ➕ 2026-10-14 🆔 fu-00000001 ^gtd-00000001f2";
const labLines = Array(43).fill("ordinary line").concat(labTask,
  labTask.replace("- [ ]", "- [x]").replace("^gtd-00000001f2", "^gtd-old"));
const row = {dataset: {gtdViewPath: "_agent/TODAY.md"}, getAttribute(name) {
  return name === "data-line" ? "0" : null;
}, querySelector(selector) {
  if (selector === ".tasks-backlink a.internal-link") return {textContent: "DEMO_PROJECT > Steps"};
  if (selector === ".task-description") return {textContent:
    "Ask sample coordinator about fictional workshop booking #computer #next"};
  return null;
}};
const plugin = {app: {metadataCache: {getFirstLinkpathDest(key) {
  return key === "DEMO_PROJECT" ? labFile : null;
}}, vault: {read: async () => labLines.join("\n")}}};
PluginClass.prototype.resolveTask.call(plugin, row).then(found => {
  assert.equal(found?.index, 43);
  assert.equal(found?.line, labTask);
  plugin.app.vault.read = async () => labLines.join("\n") + "\n" + labTask.replace("^gtd-00000001f2", "^gtd-duplicate");
  return PluginClass.prototype.resolveTask.call(plugin, row);
}).then(found => {
  assert.equal(found, null, "duplicate open descriptions must not pick a source");
  console.log("Quick Update helpers passed");
}).catch(error => {console.error(error); process.exitCode = 1;});
