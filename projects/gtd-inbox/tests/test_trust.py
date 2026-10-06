"""Item 16: kinds listed in [trust] bookkeeping apply without asking, logged and undoable. Default: none."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from helpers import VaultCase, make_settings, make_vault
from gtd_agent.housekeeping import Housekeeper

NOW = datetime(2026, 9, 28, 9, 0)
LAB = "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"
SINGLES = "01_GTD/SINGLE_ACTIONS.md"


class TrustedFollowUpTests(VaultCase):
    def extra_config(self):
        return '[trust]\nbookkeeping = ["follow_up"]'

    def test_a_trusted_kind_applies_without_a_proposal(self):
        Housekeeper(self.agent).run(now=NOW)
        self.assertEqual(self.pending("followup"), [])
        self.assertIn("- [ ] Follow up with Admin office re: room approval #computer #next", self.read(LAB))
        self.assertIn("Auto · trusted Follow-up: Follow up with Admin office", self.read("_agent/LOG.md"))
        self.assertTrue(any("Follow up with Admin office" in r["summary"] for r in self.agent.ledger.list_undo()))
        self.assertNotIn("Follow up with Admin office", self.read("_agent/APPROVAL.md"))

    def test_other_kinds_still_wait(self):
        self.add_inbox("Water the plants #anywhere")
        self.agent.scan()
        self.assertEqual(len(self.pending("inbox")), 1)


class TrustedCaptureTests(VaultCase):
    def extra_config(self):
        return '[trust]\nbookkeeping = ["next_action"]'

    def test_a_trusted_capture_is_filed_at_once_and_teaches_nothing(self):
        self.add_inbox("Water the plants #anywhere")
        self.agent.scan()
        self.assertEqual(self.pending(), [])
        self.assertIn("Water the plants #anywhere", self.read(SINGLES))
        self.assertNotIn("Water the plants", self.read("00_INBOX/INBOX.md"))
        self.assertEqual(self.agent.ledger.list_learning(), [])


class TrustConfigTests(unittest.TestCase):
    def test_an_unknown_kind_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            make_vault(base / "vault")
            with self.assertRaisesRegex(ValueError, r"\[trust\] bookkeeping: unknown kind everything"):
                make_settings(base, extra='[trust]\nbookkeeping = ["everything"]')


if __name__ == "__main__":
    unittest.main()
