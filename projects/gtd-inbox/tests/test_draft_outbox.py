"""A draft SampleOwner approves is never lost. It waits in the ledger until Bridge answers, then lands in Proton Drafts once.

The fake IMAP server stands in for Bridge: stopping it is closing Bridge, starting it is opening Bridge again.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

import test_mail
from gtd_agent.cli import main
from gtd_agent.housekeeping import Housekeeper

NOW = datetime(2026, 9, 30, 9, 0)
CONTACT = "---\ntype: contact\naliases: [Admin office]\nemails: [rooms@example.edu]\n---\n"
PAT = "---\ntype: contact\naliases: [Pat]\nemails: [pat@example.net]\n---\n"


class OutboxCase(test_mail.LabeledMailCase):
    """Two follow-ups with drafts wait in APPROVAL, one for the Admin office and one for Pat; SampleOwner ticks them."""

    def setUp(self) -> None:
        super().setUp()
        self.path("04_REFERENCE/CONTACTS").mkdir(parents=True, exist_ok=True)
        self.write("04_REFERENCE/CONTACTS/ROOM_DESK.md", CONTACT)
        self.write("04_REFERENCE/CONTACTS/PAT_LEE.md", PAT)
        waits = self.read("01_GTD/SINGLE_ACTIONS.md")
        self.write("01_GTD/SINGLE_ACTIONS.md", waits.replace("📅 2026-10-04", "📅 2026-09-28"))  # Pat's wait is due too
        self.scan(0)
        self.row = self.follow_up("rooms@example.edu")
        self.other = self.follow_up("pat@example.net")

    def scan(self, minutes: int) -> None:
        Housekeeper(self.agent).run(now=NOW + timedelta(minutes=minutes))

    def follow_ups(self) -> list:
        """The waiting follow-up proposals that carry a draft."""
        return [row for row in self.pending("followup") if json.loads(row["proposal_json"]).get("drafts")]

    def follow_up(self, address: str):
        return next(row for row in self.follow_ups() if address in row["proposal_json"])

    def status(self, row) -> str:
        return self.agent.ledger.get(row["id"])["status"]

    def today(self) -> str:
        return self.read("_agent/TODAY.md")

    def recipients(self) -> list[str]:
        return sorted(str(d["To"]) for d in self.drafts())


class ClosedBridgeTests(OutboxCase):
    def test_a_draft_approved_while_bridge_is_closed_waits_and_is_saved_once_when_it_returns(self):
        self.server.stop()
        self.approve(self.row["id"])
        self.assertEqual(self.status(self.row), "committed")  # the vault change was made; only the draft waits
        self.scan(1)
        self.assertIn("Drafts waiting for Proton: 1", self.today())
        self.assertNotIn("CHECK · a draft", self.read("_agent/LOG.md"))
        self.assertIn("waiting for Bridge", self.read("_agent/LOG.md"))
        self.server.start()
        self.scan(2)
        self.assertEqual(self.recipients(), ["Admin office <rooms@example.edu>"])
        self.assertNotIn("Drafts waiting", self.today())
        self.assertIn("Saved 1 draft to your Proton Drafts", self.read("_agent/LOG.md"))
        self.scan(3)
        self.assertEqual(len(self.drafts()), 1)  # never twice

    def test_a_draft_saved_at_once_leaves_nothing_waiting(self):
        self.approve(self.row["id"])
        self.assertEqual(len(self.drafts()), 1)
        self.scan(1)
        self.assertNotIn("Drafts waiting", self.today())
        self.assertEqual(len(self.drafts()), 1)

    def test_a_connection_that_drops_during_the_save_loses_nothing(self):
        self.server.append_script = ["drop"]
        self.approve(self.row["id"])
        self.assertEqual(self.status(self.row), "committed")
        self.assertEqual(self.drafts(), [])
        self.assertIn("waiting for Bridge", self.read("_agent/LOG.md"))  # said as a wait, not as a crash
        self.scan(1)  # the server answers properly now
        self.assertEqual(len(self.drafts()), 1)
        self.assertNotIn("Drafts waiting", self.today())

    def test_a_refusal_keeps_the_draft_and_says_why_on_today(self):
        self.server.append_script = ["no", "no"]
        self.approve(self.row["id"])
        self.scan(1)
        self.assertIn("Drafts waiting for Proton: 1", self.today())
        self.assertIn("The mailbox wouldn't take a draft", self.today())
        self.scan(2)  # the server takes it now
        self.assertEqual(len(self.drafts()), 1)

    def test_a_login_bridge_refuses_is_named_on_today_and_not_blamed_on_a_closed_bridge(self):
        with mock.patch.dict(os.environ, {"GTD_SECRET_AGENT_MAIL": "not-the-password"}):
            self.approve(self.row["id"])
            self.scan(1)
        self.assertIn("Drafts waiting for Proton: 1", self.today())
        self.assertIn("refused the login", self.today())
        self.assertNotIn("as soon as Bridge is open", self.today())
        self.scan(2)  # the right password is back
        self.assertEqual(len(self.drafts()), 1)

    def test_drafts_that_wait_are_saved_in_the_order_they_were_approved(self):
        self.server.stop()
        self.approve(self.row["id"])
        self.approve(self.other["id"])
        self.server.start()
        self.server.append_script = ["ok", "no"]  # the second one is refused this time
        self.scan(1)
        self.assertEqual(self.recipients(), ["Admin office <rooms@example.edu>"])  # the first approved arrived first
        self.assertIn("Drafts waiting for Proton: 1", self.today())
        self.scan(2)
        self.assertEqual(self.recipients(), ["Admin office <rooms@example.edu>", "Pat <pat@example.net>"])
        self.assertNotIn("Drafts waiting", self.today())


class NoBridgeTests(OutboxCase):
    bridge = False

    def test_without_bridge_the_text_stays_in_approval_and_nothing_waits(self):
        self.approve(self.row["id"])
        self.scan(1)
        self.assertEqual(self.drafts(), [])
        self.assertNotIn("Drafts waiting", self.today())


class LostDraftsTests(OutboxCase):
    """Drafts approved before the outbox existed left no record of reaching Proton. SampleOwner can list and resend them."""

    def run_command(self, *words: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--config", str(self.settings.config_path), "drafts", *words])
        return code, out.getvalue()

    def lose(self) -> None:
        """The old behavior: approved, vault changes made, the draft never kept."""
        self.agent.ledger.set_status(self.row["id"], "committed")

    def test_a_draft_that_never_reached_proton_is_listed_with_its_recipient_and_subject(self):
        self.lose()
        code, out = self.run_command("list")
        self.assertEqual(code, 0)
        self.assertIn(self.row["id"], out)
        self.assertIn("rooms@example.edu", out)
        self.assertIn("Following up", out)

    def test_resend_saves_it_and_it_is_no_longer_listed(self):
        self.lose()
        code, out = self.run_command("resend", "--id", self.row["id"])
        self.assertEqual(code, 0)
        self.assertEqual(self.recipients(), ["Admin office <rooms@example.edu>"])
        self.assertNotIn(self.row["id"], self.run_command("list")[1])
        self.assertEqual(len(self.drafts()), 1)

    def test_resend_with_bridge_closed_keeps_it_for_the_agent_to_save(self):
        self.lose()
        self.server.stop()
        code, out = self.run_command("resend", "--id", self.row["id"])
        self.assertEqual(code, 0)
        self.assertIn("waiting", out.lower())
        self.server.start()
        self.scan(1)
        self.assertEqual(len(self.drafts()), 1)

    def test_resend_all_skips_drafts_that_already_arrived(self):
        self.approve(self.row["id"])  # arrives at once
        self.agent.ledger.set_status(self.other["id"], "committed")  # lost the old way
        self.run_command("resend", "--all")
        self.assertEqual(len(self.drafts()), 2)  # the first was not sent a second time
        self.run_command("resend", "--all")
        self.assertEqual(len(self.drafts()), 2)

    def test_a_proposal_without_a_draft_is_never_listed(self):
        plain = next(row for row in self.pending() if '"mail_draft"' not in row["proposal_json"])
        self.agent.ledger.set_status(plain["id"], "committed")
        self.assertNotIn(plain["id"], self.run_command("list")[1])

    def test_an_unknown_id_is_refused_plainly(self):
        code, out = self.run_command("resend", "--id", "nosuchid")
        self.assertEqual(code, 1)
        self.assertIn("No approved draft", out)


class OutboxLedgerTests(OutboxCase):
    def test_what_waits_is_the_draft_text_and_nothing_else_of_the_mail(self):
        self.server.stop()
        self.approve(self.row["id"])
        waiting = self.agent.ledger.kv_items("draft_outbox|")
        self.assertEqual(len(waiting), 1)
        saved = json.loads(next(iter(waiting.values())))
        self.assertEqual(set(saved), {"to", "subject", "body", "in_reply_to", "queued_at"})
