"""Item 14: the agent keeps a project's ## Current state as bookkeeping, until you edit that section yourself."""
import json
import unittest
from datetime import datetime

from helpers import VaultCase
from gtd_agent.housekeeping import Housekeeper

PLANTS = "02_PROJECTS/PLANTS/PLANTS.md"
LAB = "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"
NOW = datetime(2026, 9, 28, 9, 0)
PLACEHOLDER = "_The GTD agent keeps this current._"
PLANTS_TEXT = """---
key: PLANTS
type: project
status: active
area: "[[AREA_CLUB]]"
due: 2026-11-15
priority:
aliases: []
review:
private: false
---
# PLANTS

## Outcome
Healthy plants in every room.

## Current state
{state}

## Steps
- [ ] Buy pots #errands #next
- [ ] Repot the fern #anywhere 🆔 fern
- [ ] Water the fern #anywhere ⛔ fern

## Waiting For
- [ ] #waiting Sam: spare soil ➕ 2026-09-25 📅 2026-10-02

## Open questions
- Which fertilizer?

## Log
- 2026-09-27 Picked the rooms.
"""
EXPECTED = ("- Next: Buy pots\n"
            "- Waiting on: Sam: spare soil (follow up 2026-10-02)\n"
            "- Blocked: Water the fern waits on Repot the fern\n"
            "- Open questions: 1\n"
            "- Due: 2026-11-15\n"
            "- Last change: 2026-09-27 Picked the rooms.")


class StateCase(VaultCase):
    state = PLACEHOLDER

    def extra_files(self):
        return {PLANTS: PLANTS_TEXT.format(state=self.state)}

    def keep(self):
        return Housekeeper(self.agent).run(now=NOW)

    def section(self):
        text = self.read(PLANTS)
        return text.split("## Current state\n", 1)[1].split("\n\n## ", 1)[0].strip()

    def edit(self, old, new):
        text = self.read(PLANTS)
        self.assertIn(old, text)
        self.write(PLANTS, text.replace(old, new, 1))


class AgentKeptTests(StateCase):
    def test_the_placeholder_becomes_the_current_state(self):
        self.keep()
        self.assertEqual(self.section(), EXPECTED)
        self.assertIn("Auto · Current state of PLANTS", self.read("_agent/LOG.md"))
        self.assertTrue(any("Current state of PLANTS" in r["summary"] for r in self.agent.ledger.list_undo()))

    def test_a_change_rewrites_it(self):
        self.keep()
        self.edit("- [ ] Buy pots #errands #next", "- [x] Buy pots #errands #next ✅ 2026-09-28")
        self.keep()  # promotion tags the next step
        self.keep()
        self.assertTrue(self.section().startswith("- Next: Repot the fern\n"))
        states = [r for r in self.agent.ledger.list_undo(limit=30) if "Current state of PLANTS" in r["summary"]]
        self.assertEqual(len(states), 1)  # a newer rewrite replaces the older Undo

    def test_a_hand_edit_is_respected_until_the_placeholder_returns(self):
        self.keep()
        self.edit(EXPECTED, "- Next: repot everything this weekend")
        self.keep()
        self.edit("- [ ] Buy pots #errands #next", "- [x] Buy pots #errands #next ✅ 2026-09-28")
        self.keep()
        self.assertEqual(self.section(), "- Next: repot everything this weekend")
        self.assertIn("Current state: you edited it in [[PLANTS]]", self.read("_agent/TODAY.md"))
        self.edit("- Next: repot everything this weekend", PLACEHOLDER)
        self.keep()
        self.assertTrue(self.section().startswith("- Next: Repot the fern"))

    def test_long_lists_stop_at_three(self):
        self.edit("- [ ] Repot the fern #anywhere 🆔 fern", "- [ ] Repot the fern #anywhere #next 🆔 fern\n"
                  "- [ ] Feed the palm #anywhere #next\n- [ ] Dust the leaves #anywhere #next")
        self.keep()
        self.assertTrue(self.section().startswith("- Next: Buy pots · Repot the fern · Feed the palm (+1 more)\n"))

    def test_a_parked_project_shows_its_status_not_a_missing_step(self):
        self.edit("status: active", "status: someday")
        self.edit("- [ ] Buy pots #errands #next", "- [ ] Buy pots #errands")
        self.keep()
        self.assertTrue(self.section().startswith("- Status: someday\n- Waiting on:"))

    def test_a_note_without_the_section_is_left_alone(self):
        self.keep()
        self.assertNotIn("## Current state", self.read(LAB))


class ReviewWrittenTests(StateCase):
    state = "Seeds bought; waiting on soil from Sam."

    def test_review_written_text_is_replaced_once_through_approval(self):
        self.keep()
        self.assertEqual(self.section(), "Seeds bought; waiting on soil from Sam.")
        (row,) = self.pending("state")
        proposal = json.loads(row["proposal_json"])
        self.assertEqual(proposal["kind"], "current_state")
        self.assertIn("## Current state handovers", self.read("_agent/APPROVAL.md"))
        self.assertIn(f"### Current state handovers (1)\n- Approve to let the agent keep the section, reject to keep "
                      f"yours: [[APPROVAL#^p-{row['id']}|PLANTS]]", self.read("_agent/TODAY.md"))
        self.tick(proposal_id=row["id"])
        self.agent.sync_review_requests()
        self.assertEqual(self.section(), EXPECTED)
        self.edit("- [ ] Buy pots #errands #next", "- [x] Buy pots #errands #next ✅ 2026-09-28")
        self.keep()
        self.keep()
        self.assertTrue(self.section().startswith("- Next: Repot the fern"))
        self.assertEqual(self.pending("state"), [])

    def test_rejecting_the_replacement_keeps_it_yours(self):
        self.keep()
        (row,) = self.pending("state")
        self.tick("Reject", proposal_id=row["id"])
        self.agent.sync_review_requests()
        self.edit("- [ ] Buy pots #errands #next", "- [x] Buy pots #errands #next ✅ 2026-09-28")
        self.keep()
        self.keep()
        self.assertEqual(self.section(), "Seeds bought; waiting on soil from Sam.")
        self.assertEqual(self.pending("state"), [])


if __name__ == "__main__":
    unittest.main()
