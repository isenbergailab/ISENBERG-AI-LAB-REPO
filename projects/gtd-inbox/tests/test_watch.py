"""Item 13: hand edits to project, area and list notes are watched. Ticks, cancels, dates and status follow
rules; new decisions, answered questions and outcome changes go to GLM and come back as proposals."""
import json
import os
import time
import unittest
from datetime import datetime

from helpers import VaultCase
from gtd_agent.housekeeping import Housekeeper

GARDEN = "02_PROJECTS/GARDEN/GARDEN.md"
NOW = datetime(2026, 9, 28, 9, 0)
GARDEN_TEXT = """---
key: GARDEN
type: project
status: active
area: "[[AREA_CLUB]]"
due:
priority:
aliases: [Garden]
review:
private: false
---
# GARDEN

## Outcome
A vegetable bed by May.

## Steps
- [ ] Measure the yard #errands #next
- [ ] Buy seeds #errands
- [ ] Build a raised bed #anywhere

## Decisions
- **Grow tomatoes** · they grow easily · 2026-09-20

## Open questions
- Where should the bed go?
- Which soil mix?

## Log
- 2026-09-20 Started.
"""
DECIDED = {"decision": "Put the bed by the fence", "why": "it gets sun", "question_ids": ["q1"], "section": "",
           "steps": [], "explanation": "It settles where the bed goes."}


class WatchCase(VaultCase):
    remote = True
    private = False

    def extra_files(self):
        text = GARDEN_TEXT.replace("private: false", "private: true") if self.private else GARDEN_TEXT
        return {GARDEN: text}

    def keep(self):
        return Housekeeper(self.agent).run(now=NOW)

    def model_calls(self):
        return [name for name, _ in self.provider.calls if name in {"decided", "outcome", "interpret", "decide"}]

    def edit(self, old, new):
        text = self.read(GARDEN)
        self.assertIn(old, text)
        self.write(GARDEN, text.replace(old, new, 1))

    def proposals(self):
        return [(row, json.loads(row["proposal_json"])) for row in self.pending("watch")]


class RuleEditTests(WatchCase):
    def test_an_unchanged_vault_costs_no_model_calls(self):
        self.keep()
        self.keep()
        self.assertEqual(self.model_calls(), [])

    def test_ticks_cancels_dates_and_status_follow_rules(self):
        self.keep()
        self.edit("- [ ] Measure the yard #errands #next", "- [x] Measure the yard #errands #next ✅ 2026-09-28")
        self.edit("- [ ] Buy seeds #errands", "- [-] Buy seeds #errands ❌ 2026-09-28")
        self.edit("- [ ] Build a raised bed #anywhere", "- [ ] Build a raised bed #anywhere 📅 2026-10-10")
        self.edit("status: active", "status: waiting")
        self.keep()
        self.assertEqual(self.model_calls(), [])
        self.assertEqual(self.proposals(), [])

    def test_a_moved_decision_is_not_a_new_one(self):
        self.edit("## Decisions\n- **Grow tomatoes** · they grow easily · 2026-09-20\n",
                  "## Decisions\n- **Water daily** · sun is strong · 2026-09-21\n"
                  "- **Grow tomatoes** · they grow easily · 2026-09-20\n")
        self.keep()
        self.edit("- **Water daily** · sun is strong · 2026-09-21\n- **Grow tomatoes** · they grow easily · 2026-09-20\n",
                  "- **Grow tomatoes** · they grow easily · 2026-09-20\n- **Water daily** · sun is strong · 2026-09-21\n")
        self.keep()
        self.assertEqual(self.model_calls(), [])

    def test_a_reworded_decision_is_not_a_new_one(self):
        self.keep()
        self.edit("they grow easily", "they grow very easily")
        self.keep()
        self.assertEqual(self.model_calls(), [])


class JudgmentEditTests(WatchCase):
    def test_a_new_decision_asks_which_questions_it_settles(self):
        self.keep()
        self.edit("- **Grow tomatoes** · they grow easily · 2026-09-20",
                  "- **Grow tomatoes** · they grow easily · 2026-09-20\n"
                  "- **Put the bed by the fence** · it gets sun · 2026-09-28")
        self.provider.decided_answers.append(dict(DECIDED))
        self.keep()
        call = next(value for name, value in self.provider.calls if name == "decided")
        self.assertEqual(call["capture"], "Put the bed by the fence · it gets sun")
        (row, proposal), = self.proposals()
        self.assertEqual(proposal["kind"], "decision")
        self.assertNotIn("add_decision", [op["op"] for op in proposal["ops"]])  # you wrote it already
        self.tick(proposal_id=row["id"])
        self.agent.sync_review_requests()
        garden = self.read(GARDEN)
        self.assertNotIn("Where should the bed go?", garden)
        self.assertIn("## Log\n- 2026-09-28 Decided: Put the bed by the fence", garden)
        self.keep()  # the agent's own write is not an edit
        self.assertEqual(self.model_calls(), ["decided"])

    def test_an_answered_question_becomes_a_decision(self):
        self.keep()
        self.edit("- Which soil mix?", "- Which soil mix? Half compost, half topsoil, it drains well")
        self.provider.decided_answers.append({"decision": "Half compost, half topsoil", "why": "it drains well",
                                              "question_ids": [], "section": "", "steps": [],
                                              "explanation": "The answer settles the soil."})
        self.keep()
        (row, proposal), = self.proposals()
        self.tick(proposal_id=row["id"])
        self.agent.sync_review_requests()
        garden = self.read(GARDEN)
        self.assertIn("- **Half compost, half topsoil** · it drains well · 2026-09-28", garden)
        self.assertNotIn("Which soil mix?", garden)

    def test_an_outcome_change_checks_the_steps(self):
        self.keep()
        self.edit("A vegetable bed by May.", "A herb garden by April.")
        self.provider.outcome_answers.append(lambda steps: {
            "drop_step_ids": [s["id"] for s in steps if "raised bed" in s["text"]],
            "new_steps": [{"title": "Pick five herbs", "context": "#anywhere"}],
            "explanation": "Herbs need no raised bed."})
        self.keep()
        call = next(value for name, value in self.provider.calls if name == "outcome")
        self.assertEqual((call["old"], call["new"]), ("A vegetable bed by May.", "A herb garden by April."))
        (row, proposal), = self.proposals()
        labels = {item["label"]: item["checked"] for item in proposal["items"]}
        self.assertFalse(labels["Drop step · Build a raised bed"])
        self.assertTrue(next(checked for label, checked in labels.items() if label.startswith("Step · Pick five herbs")))
        number = list(labels).index("Drop step · Build a raised bed") + 1
        self.tick(f"{number}. Drop step", proposal_id=row["id"])
        self.tick(proposal_id=row["id"])
        self.agent.sync_review_requests()
        garden = self.read(GARDEN)
        self.assertIn("- [-] Build a raised bed #anywhere ❌ 2026-09-28", garden)
        self.assertIn("Pick five herbs #anywhere", garden)
        self.assertIn("- 2026-09-28 Outcome changed to: A herb garden by April.", garden)
        self.keep()
        self.assertEqual(self.pending("drop"), [])  # the agent dropped it, so it does not ask why


class PrivateEditTests(WatchCase):
    private = True

    def test_a_private_project_gets_rules_only(self):
        self.keep()
        self.edit("- Which soil mix?", "- Which soil mix? Half compost")
        self.edit("A vegetable bed by May.", "A herb garden by April.")
        self.keep()
        self.assertEqual(self.model_calls(), [])
        self.assertEqual(self.proposals(), [])


class QuietEditTests(WatchCase):
    quiet = 10

    def test_an_edit_is_read_once_the_note_is_quiet(self):
        self.keep()
        self.edit("- **Grow tomatoes** · they grow easily · 2026-09-20",
                  "- **Grow tomatoes** · they grow easily · 2026-09-20\n"
                  "- **Put the bed by the fence** · it gets sun · 2026-09-28")
        self.provider.decided_answers.append(dict(DECIDED))
        self.keep()
        self.assertEqual(self.model_calls(), [])
        old = time.time() - 11 * 60
        os.utime(self.path(GARDEN), (old, old))
        self.keep()
        self.assertEqual(self.model_calls(), ["decided"])


if __name__ == "__main__":
    unittest.main()
