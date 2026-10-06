"""Item 12: each LINT rule is safe (fixed on its own, logged, undoable) or a choice (one tick in APPROVAL,
linked from TODAY). LINT.md lists what is left."""
import os
import time
import unittest
from datetime import datetime

from helpers import VaultCase
from gtd_agent.housekeeping import Housekeeper

SINGLES = "01_GTD/SINGLE_ACTIONS.md"
LAB = "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"
SAILING = "02_PROJECTS/SAILING/SAILING.md"
NOW = datetime(2026, 9, 28, 9, 0)


class LintCase(VaultCase):
    def lint(self):
        return Housekeeper(self.agent).run(now=NOW)

    def choice(self, key_part):
        rows = [r for r in self.pending("lint") if key_part in r["dedupe_key"]]
        self.assertEqual(len(rows), 1, [r["dedupe_key"] for r in self.pending("lint")])
        return rows[0]

    def controls(self, row):
        text = self.read("_agent/APPROVAL.md")
        start = text.index(f"<!-- gtd-agent:proposal id={row['id']} -->")
        block = text[start:text.index("<!-- gtd-agent:item-end -->", start)]
        tail = block.split("**Decision (choose one):**", 1)[1]
        return [line[6:] for line in tail.split("\n") if line.startswith("- [ ] ")]

    def answer(self, row, label):
        self.tick(label, proposal_id=row["id"], suffix=" ✅ 2026-09-28")
        return self.agent.sync_review_requests()


class SafeFixTests(LintCase):
    def extra_files(self):
        return {SINGLES: "# SINGLE_ACTIONS\n\n## @computer\n- [ ] Fix bike #errands\n- [ ] Fix bike #errands\n"
                         "- [ ] Read [[The Demo]] notes #computer\n- [ ] Call Bob 📅 2026-10-01 about rent #calls\n",
                SAILING: "---\nkey: SAILING\ntype: project\nstatus: someday\narea: \"[[AREA_CLUB]]\"\n---\n# SAILING\n\n"
                         "## Steps\n- [ ] Find a class #computer #next\n- [ ] Buy gloves #errands\n"}

    def test_safe_fixes_apply_on_their_own_logged_and_undoable(self):
        self.lint()
        singles = self.read(SINGLES)
        self.assertEqual(singles.count("Fix bike"), 1)
        self.assertIn("- [ ] Read [[DEMO_PROJECT|The Demo]] notes #computer", singles)
        self.assertIn("- [ ] Call Bob about rent #calls 📅 2026-10-01", singles)
        self.assertIn("- [ ] Find a class #computer\n", self.read(SAILING))
        self.assertEqual(self.read("_agent/LOG.md").count("Auto · Vault fix"), 4)
        self.assertEqual(len(self.agent.ledger.list_undo()), 4)
        self.assertEqual(self.pending("lint"), [])
        report = self.read("_agent/LINT.md")
        for fixed in ("Fix bike", "The Demo", "Call Bob", "SAILING"):
            self.assertNotIn(fixed, report)

    def test_an_undone_fix_is_not_redone(self):
        self.lint()
        record = self.agent.ledger.list_undo()[0]  # the newest: text moved before the dates
        self.assertIn("Moved text before the dates", record["summary"])
        self.agent.undo(record["id"])
        self.assertIn("- [ ] Call Bob 📅 2026-10-01 about rent #calls", self.read(SINGLES))
        self.lint()
        self.assertIn("- [ ] Call Bob 📅 2026-10-01 about rent #calls", self.read(SINGLES))
        self.assertIn("about rent #calls` · [[SINGLE_ACTIONS]] · you undid the agent's fix",
                      self.read("_agent/LINT.md"))


class QuietTests(LintCase):
    quiet = 10

    def extra_files(self):
        return {SINGLES: "# SINGLE_ACTIONS\n\n## @computer\n- [ ] Fix bike #errands\n- [ ] Fix bike #errands\n"
                         "- [ ] Sort mail\n"}

    def test_a_note_edited_in_the_last_ten_minutes_is_left_alone(self):
        self.lint()
        self.assertEqual(self.read(SINGLES).count("Fix bike"), 2)
        self.assertEqual(self.pending("lint"), [])
        self.assertIn("Sort mail", self.read("_agent/LINT.md"))  # still reported
        old = time.time() - 11 * 60
        os.utime(self.path(SINGLES), (old, old))
        self.lint()
        self.assertEqual(self.read(SINGLES).count("Fix bike"), 1)
        self.choice("lintctx")


class ChoiceTests(LintCase):
    def extra_files(self):
        return {SINGLES: "# SINGLE_ACTIONS\n\n## @computer\n- [ ] Fix bike\n\n## Waiting For\n"
                         "- [ ] #waiting Pat: slides ➕ 2026-09-27\n"}

    def test_an_action_without_a_context_asks_for_one(self):
        self.lint()
        row = self.choice("lintctx")
        self.assertEqual(self.controls(row),
                         ["Tag #computer", "Tag #calls", "Tag #anywhere", "Tag #errands", "Leave it"])
        today = self.read("_agent/TODAY.md")
        self.assertIn("### Vault choices (2)", today)
        self.assertEqual(today.count("[[APPROVAL#^p-"), 3)  # 2 choices + 1 follow-up proposal, each listed once
        self.assertIn(f"- [[APPROVAL#^p-{row['id']}|Pick a context · Fix bike]]", today)
        self.assertIn(f"[[APPROVAL#^p-{row['id']}|answer]]", self.read("_agent/LINT.md"))
        self.answer(row, "Tag #errands")
        self.assertIn("- [ ] Fix bike #errands\n", self.read(SINGLES))
        self.lint()
        self.assertNotIn("Fix bike", self.read("_agent/LINT.md"))

    def test_a_wait_without_a_follow_up_date_asks_when(self):
        self.lint()
        row = self.choice("lintwait")
        self.assertEqual(self.controls(row),
                         ["Follow up in 3 days", "Follow up in a week", "Follow up in 2 weeks", "Leave it"])
        self.answer(row, "Follow up in a week")
        self.assertIn("- [ ] #waiting Pat: slides ➕ 2026-09-27 📅 2026-10-05", self.read(SINGLES))

    def test_leave_it_stops_asking_and_clears_lint(self):
        self.lint()
        self.answer(self.choice("lintctx"), "Leave it")
        self.lint()
        self.assertEqual([r for r in self.pending("lint") if "lintctx" in r["dedupe_key"]], [])
        self.assertNotIn("Fix bike", self.read("_agent/LINT.md"))
        self.assertIn("- [ ] Fix bike\n", self.read(SINGLES))


class ProjectChoiceTests(LintCase):
    def test_an_active_project_without_a_next_step_offers_its_first_step(self):
        self.write(LAB, self.read(LAB).replace("Email the advisor #computer #next", "Email the advisor #computer"))
        self.lint()
        row = self.choice("lintnonext|DEMO_PROJECT")
        self.assertEqual(self.controls(row), ["Tag first step #next", "Set status waiting", "Set status someday",
                                              "Set status done", "Leave it"])
        self.answer(row, "Tag first step #next")
        self.assertIn("- [ ] Email the advisor #next #computer", self.read(LAB))

    def test_a_past_due_project_offers_new_dates(self):
        self.write(LAB, self.read(LAB).replace("due: 2026-12-01", "due: 2026-09-01"))
        self.lint()
        row = self.choice("lintdue|DEMO_PROJECT")
        self.assertEqual(self.controls(row), ["Due in a week", "Due in a month", "Clear the due date",
                                              "Set status done", "Leave it"])
        self.answer(row, "Due in a week")
        self.assertIn("\ndue: 2026-10-05\n", self.read(LAB))
        self.lint()
        self.assertNotIn("has passed", self.read("_agent/LINT.md"))

    def test_same_task_in_two_notes_asks_which_to_keep(self):
        self.write(SINGLES, self.read(SINGLES).replace("- [ ] Renew library card #computer",
                                                       "- [ ] Renew library card #computer\n"
                                                       "- [ ] Email the advisor #computer"))
        self.lint()
        row = self.choice("lintdupe")
        self.assertEqual(self.controls(row), ["Keep the one in SINGLE_ACTIONS", "Keep the one in DEMO_PROJECT",
                                              "Leave it"])
        self.answer(row, "Keep the one in DEMO_PROJECT")
        self.assertIn("Email the advisor #computer #next", self.read(LAB))
        self.assertNotIn("Email the advisor", self.read(SINGLES))


if __name__ == "__main__":
    unittest.main()
