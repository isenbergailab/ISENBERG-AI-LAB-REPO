"use strict";
const assert = require("node:assert/strict");
const {test} = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const gre = require("./core.js");

const SOURCE = "04_REFERENCE/STUDY/INCORRECT_ANSWERS_REVIEW.md";
const PRACTICE = "04_REFERENCE/STUDY/STUDY_QUESTION_REVIEW_PRACTICE.md";
const ARCHIVE = "04_REFERENCE/STUDY/STUDY_QUESTION_REVIEW_ARCHIVE.md";
let clock = new Date("2026-10-20T15:00:00Z");
class TestDate extends Date {
  constructor(...args) {super(...(args.length ? args : [clock.getTime()]));}
  static now() {return clock.getTime();}
}
const sandbox = {module:{exports:{}}, Date:TestDate, Intl, console,
  require(name) {
    if (name === "obsidian") return {Plugin:class {}, MarkdownView:class {}, Modal:class {},
      Notice:class {constructor() {}}, Setting:class {}};
    if (name === "./core.js") return gre;
    throw new Error(name);
  }};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "main.js"), "utf8"), sandbox);
const Plugin = sandbox.module.exports;

function fakeVault(contents) {
  const files = new Map(Object.entries(contents).map(([p,text]) =>
    [p,{path:p,text,stat:{mtime:clock.getTime()}}]));
  return {files,
    getAbstractFileByPath(p) {return files.get(p) || null;},
    async read(file) {return file.text;},
    async create(p,text) {files.set(p,{path:p,text,stat:{mtime:clock.getTime()}});},
    async process(file,fn) {file.text=fn(file.text); file.stat.mtime=clock.getTime();},
  };
}
function sample() {
  let note=gre.addTemplate("", "2026-10-17", "gre-sample");
  note=note.replace("- Question: ![[]]", "- Question: ![[question.png]]")
    .replace("- Mine: ", "- Mine: B")
    .replace("- Right: ", "- Right: C")
    .replace("- Explanation: ![[]]", "- Explanation: ![[explanation.png]]")
    .replace("- Why: ", "- Why: Missed clue");
  return gre.ready(note,"gre-sample","2026-10-17");
}

test("Plugin grades locally, reveals explanation, then clears after quiet time", async () => {
  clock = new Date("2026-10-20T15:00:00Z");
  const vault=fakeVault({[SOURCE]:sample()});
  const plugin=new Plugin();
  plugin.app={vault}; plugin.scheduleDecorate=()=>{};
  await plugin.initialize();
  assert.doesNotMatch(vault.files.get(PRACTICE).text,/explanation\.png/);
  await plugin.gradeQuestion(1,"wrong");
  assert.match(vault.files.get(PRACTICE).text,/explanation\.png/);
  assert.equal(gre.parseEntries(vault.files.get(SOURCE).text)[0].streak,0);
  vault.files.get(PRACTICE).stat.mtime-=10*60*1000;
  await plugin.clearIdle();
  assert.doesNotMatch(vault.files.get(PRACTICE).text,/explanation\.png/);
  assert.match(vault.files.get(SOURCE).text,/scheduled 2026-10-20/);
  assert.match(vault.files.get(ARCHIVE).text,/Archive/);
});

test("Second correct grade archives the full record; Undo restores it", async () => {
  clock = new Date("2026-10-20T15:00:00Z");
  const vault=fakeVault({[SOURCE]:sample()});
  const plugin=new Plugin();
  plugin.app={vault}; plugin.scheduleDecorate=()=>{};
  await plugin.initialize();
  await plugin.gradeQuestion(1,"correct");
  clock = new Date("2026-10-24T15:00:00Z");
  await plugin.gradeQuestion(1,"correct");
  assert.equal(gre.parseEntries(vault.files.get(SOURCE).text).length,0);
  const archived=gre.parseEntries(vault.files.get(ARCHIVE).text)[0];
  assert.equal(archived.streak,2);
  assert.equal(archived.attempts.length,2);
  await plugin.undoLast();
  assert.equal(gre.parseEntries(vault.files.get(ARCHIVE).text).length,0);
  assert.equal(gre.parseEntries(vault.files.get(SOURCE).text)[0].streak,1);
});
