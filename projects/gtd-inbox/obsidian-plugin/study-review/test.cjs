"use strict";
const assert = require("node:assert/strict");
const {test} = require("node:test");
const gre = require("./core.js");

const image = "![[question.png]]";
const explanation = "![[explanation.png]]";
function readySample() {
  let note = gre.addTemplate("", "2026-10-17", "gre-sample");
  note = note.replace("- Question: ![[]]", `- Question: ${image}`)
    .replace("- Mine: ", "- Mine: B")
    .replace("- Right: ", "- Right: C")
    .replace("- Explanation: ![[]]", `- Explanation: ${explanation}`)
    .replace("- Why: ", "- Why: Vocab gap");
  return gre.ready(note, "gre-sample", "2026-10-17");
}

test("Ready requires images and answers, then dates reviews", () => {
  const draft = gre.addTemplate("", "2026-10-17", "gre-sample");
  assert.throws(() => gre.ready(draft, "gre-sample", "2026-10-17"), /Question image/);
  const entry = gre.parseEntries(readySample())[0];
  assert.deepEqual(entry.slots, ["2026-10-20", "2026-10-24"]);
  assert.equal(entry.ready, "2026-10-17");
});

test("Overdue slots accumulate, with one grade daily", () => {
  const first = gre.grade(readySample(), "gre-sample", "wrong", "2026-10-26T10:00:00-04:00");
  const entry = gre.parseEntries(first.note)[0];
  assert.deepEqual(entry.slots, ["2026-10-24", "2026-10-29"]);
  assert.equal(entry.streak, 0);
  assert.throws(() => gre.grade(first.note, "gre-sample", "correct", "2026-10-26T11:00:00-04:00"), /once per day/);
  const second = gre.grade(first.note, "gre-sample", "correct", "2026-10-27T10:00:00-04:00");
  assert.equal(gre.parseEntries(second.note)[0].streak, 1);
});

test("Second consecutive correct archives, and undo restores", () => {
  const first = gre.grade(readySample(), "gre-sample", "correct", "2026-10-20T10:00:00-04:00");
  const second = gre.grade(first.note, "gre-sample", "correct", "2026-10-24T10:00:00-04:00");
  assert.equal(second.archive, true);
  const archived = gre.parseEntries(second.note)[0];
  assert.equal(archived.streak, 2);
  assert.deepEqual(archived.slots, []);
  const undone = gre.undo(second.note, "gre-sample");
  assert.equal(gre.parseEntries(undone)[0].streak, 1);
  assert.deepEqual(gre.parseEntries(undone)[0].slots, ["2026-10-24"]);
});

test("A correct grade schedules seven days when no slot remains", () => {
  const sample = readySample().replace("2026-10-20, 2026-10-24", "2026-10-20");
  const result = gre.grade(sample, "gre-sample", "correct", "2026-10-20T10:00:00-04:00");
  assert.deepEqual(gre.parseEntries(result.note)[0].slots, ["2026-10-27"]);
});

test("Practice reveals explanations only after grading, then clears reviewed cards", () => {
  const before = gre.renderQueue(gre.parseEntries(readySample()), "2026-10-20");
  assert.match(before, /question\.png/);
  assert.doesNotMatch(before, /explanation\.png|Right: C/);
  const outcome = gre.grade(readySample(), "gre-sample", "wrong", "2026-10-20T10:00:00-04:00");
  const reviewed = gre.reviewedCard(gre.parseEntries(outcome.note)[0], "wrong", "2026-10-20");
  assert.match(reviewed, /explanation\.png/);
  const practice = gre.makePractice(before, reviewed);
  assert.match(gre.clearReviewed(practice), /question\.png/);
  assert.doesNotMatch(gre.clearReviewed(practice), /explanation\.png/);
});

test("Manual dates remain editable", () => {
  const changed = gre.setDates(readySample(), "gre-sample", ["2026-10-19", "2026-10-25"]);
  assert.deepEqual(gre.parseEntries(changed)[0].slots, ["2026-10-19", "2026-10-25"]);
});

test("Archive transfer retains the complete entry", () => {
  const original = readySample();
  const entry = gre.parseEntries(original)[0];
  const archive = gre.appendEntry("# Archive\n", entry.raw);
  assert.match(archive, /explanation\.png/);
  assert.equal(gre.parseEntries(gre.removeEntry(original, entry.id)).length, 0);
  assert.equal(gre.parseEntries(archive)[0].id, entry.id);
});

test("Undo removes the latest revealed explanation only", () => {
  const entry = gre.parseEntries(readySample())[0];
  let practice = gre.makePractice("## Due\n\nNothing due.\n", "");
  practice = gre.appendReviewed(practice, gre.reviewedCard(entry, "wrong", "2026-10-20"));
  practice = gre.appendReviewed(practice, gre.reviewedCard(entry, "correct", "2026-10-21"));
  const undone = gre.removeReviewedCard(practice, entry.id);
  assert.match(undone, /Wrong · 2026-10-20/);
  assert.doesNotMatch(undone, /Correct · 2026-10-21/);
});

test("Reviewed answers clear after ten quiet minutes", () => {
  const card = gre.reviewedCard(gre.parseEntries(readySample())[0], "wrong", "2026-10-20");
  const practice = gre.makePractice("## Due\n\nNothing due.\n", card);
  assert.equal(gre.shouldClearReviewed(practice, 1000, 1000 + 599999), false);
  assert.equal(gre.shouldClearReviewed(practice, 1000, 1000 + 600000), true);
  assert.equal(gre.shouldClearReviewed(gre.clearReviewed(practice), 1000, 1000 + 600000), false);
});

test("Question numbers continue past archived entries", () => {
  const added = gre.addTemplate("", "2026-10-25", "gre-next", 8);
  assert.match(added, /^## Q8 · 2026-10-25/);
});

test("New template stays beneath note properties", () => {
  const note="---\nproject: \"[[STUDY_STUDYING]]\"\n---\n## Q1 · 2026-10-17\n";
  const added=gre.addTemplate(note,"2026-10-25","gre-next");
  assert.match(added,/^---\nproject:.*\n---\n## Q2/);
});
