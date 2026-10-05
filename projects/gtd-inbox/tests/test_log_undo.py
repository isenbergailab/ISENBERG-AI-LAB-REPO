"""APPROVAL holds only decisions waiting for you; applied changes list in LOG, newest first, each with its Undo box."""
import re
import unittest

from helpers import VaultCase
from gtd_agent.core import REVIEW_BEGIN, REVIEW_END

SINGLES = "01_GTD/SINGLE_ACTIONS.md"


class LogUndoTests(VaultCase):
    def approve_capture(self, line="Buy stamps #errands"):
        self.add_inbox(line)
        self.agent.scan()
        self.tick()
        self.agent.sync_review_requests()

    def test_approval_holds_only_pending_decisions(self):
        self.approve_capture()
        approval = self.read("_agent/APPROVAL.md")
        self.assertNotIn("gtd-agent:undo", approval)
        self.assertNotIn("- [ ] Undo", approval)
        self.assertIn("[[LOG]]", approval)

    def test_log_lists_the_applied_change_with_its_undo_box(self):
        self.approve_capture()
        log = self.read("_agent/LOG.md")
        block = log.split(REVIEW_BEGIN)[1].split(REVIEW_END)[0]
        self.assertIn("### Undo · Next action: Buy stamps", block)
        self.assertIn("- [ ] Undo", block)
        history = log.split("## History\n")[1]
        self.assertTrue(history.startswith("- "), history)
        self.assertIn("Approved · Next action: Buy stamps → Single action › @errands", history)

    def test_ticking_undo_in_log_reverses_the_change(self):
        self.approve_capture()
        self.assertIn("Buy stamps", self.read(SINGLES))
        self.tick("Undo")
        outcomes = self.agent.sync_review_requests()
        self.assertEqual(outcomes, ["Reversed: Next action: Buy stamps"])
        self.assertNotIn("Buy stamps", self.read(SINGLES))
        log = self.read("_agent/LOG.md")
        self.assertNotIn("- [ ] Undo", log)
        self.assertIn("Undone · Next action: Buy stamps", log.split("## History\n")[1].split("\n")[0])

    def test_a_tick_survives_new_log_lines_written_before_it_is_read(self):
        self.approve_capture()
        self.tick("Undo")
        self.agent.log("Something else happened")
        self.assertIn("- [x] Undo", self.read("_agent/LOG.md"))
        self.assertEqual(self.agent.sync_review_requests(), ["Reversed: Next action: Buy stamps"])

    def test_newest_change_comes_first(self):
        self.approve_capture("Buy stamps #errands")
        self.approve_capture("Fix the bike #errands")
        block = self.read("_agent/LOG.md").split(REVIEW_BEGIN)[1]
        self.assertLess(block.index("Fix the bike"), block.index("Buy stamps"))

    def test_an_old_log_without_the_undo_block_keeps_its_history(self):
        self.path("_agent").mkdir()
        self.write("_agent/LOG.md", "# LOG\n\nWhat the GTD agent changed, newest first. Undo recent changes in "
                                    "[[APPROVAL]].\n\n- 2026-09-29 16:38 Approved · Inbox file: an old change\n")
        self.approve_capture()
        log = self.read("_agent/LOG.md")
        history = log.split("## History\n")[1].split("\n")
        self.assertIn("Approved · Next action: Buy stamps", history[0])
        self.assertEqual(history[1], "- 2026-09-29 16:38 Approved · Inbox file: an old change")
        self.assertEqual(log.count("# LOG"), 1)


if __name__ == "__main__":
    unittest.main()
