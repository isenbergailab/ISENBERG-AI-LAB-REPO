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
