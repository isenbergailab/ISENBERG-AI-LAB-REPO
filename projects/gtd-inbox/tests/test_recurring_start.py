"""Item 11: a capture that creates a repeating duty asks when each copy should start showing."""
import unittest

from helpers import VaultCase, draft

SINGLES = "01_GTD/SINGLE_ACTIONS.md"
CLUB = "03_AREAS/AREA_CLUB.md"


class RecurringStartTests(VaultCase):
    remote = True

    def extra_files(self):
        return {CLUB: "---\nkey: AREA_CLUB\ntype: area\naliases: [Club]\nprivate: false\n---\n# AREA_CLUB\n\n"
                      "## Recurring\n"}

    def propose(self, capture, answer=None):
        self.add_inbox(capture)
        if answer is not None:
            self.provider.interpretations.append(answer)
        found = self.agent.scan().new_proposals
        self.assertEqual(len(found), 1)
        return found[0]

    def controls(self):
        text = self.read("_agent/APPROVAL.md")
        tail = text.split("**Decision (choose one):**", 1)[1]
        return [line[6:] for line in tail.split("\n") if line.startswith("- [ ] ")]

    def duty(self, rel, title):
        return next(line for line in self.read(rel).splitlines() if title in line)

    def test_the_demo_club_agenda_can_show_on_its_due_day(self):
        self.propose("DEMO_CLUB agenda every Monday by 1:30pm for the club", draft(
            title="Prepare the DEMO_CLUB agenda by 1:30pm", recurrence="every week on Monday", area_key="AREA_CLUB"))
        self.assertEqual(self.controls()[:3], ["Approve · show on the due day", "Approve · show 1 day before",
                                               "Approve · show 2 days before (as shown)"])
        self.assertIn("When should each copy start showing?", self.read("_agent/APPROVAL.md"))
        self.tick("Approve · show on the due day", suffix=" ✅ 2026-09-28")
        self.agent.sync_review_requests()
        line = self.duty(CLUB, "DEMO_CLUB agenda")
        self.assertIn("🔁 every week on Monday 🛫 2026-09-28 📅 2026-09-28", line)

    def test_vault_syntax_asks_too(self):
        self.propose("Water the plants #anywhere 🔁 every week on Friday")
        self.tick("Approve · show 1 day before")
        self.agent.sync_review_requests()
        self.assertIn("🛫 2026-10-01 📅 2026-10-02", self.duty(SINGLES, "Water the plants"))

    def test_plain_approve_keeps_the_start_shown(self):
        found = self.propose("Water the plants #anywhere 🔁 every week on Friday")
        self.agent.approve(found["id"])
        self.assertIn("🛫 2026-09-30 📅 2026-10-02", self.duty(SINGLES, "Water the plants"))

    def test_a_monthly_duty_offers_longer_leads(self):
        self.propose("Pay the club dues #computer 🔁 every month on the 15th")
        self.assertEqual(self.controls()[:4], ["Approve · show on the due day", "Approve · show 2 days before",
                                               "Approve · show 5 days before (as shown)",
                                               "Approve · show 2 weeks before"])

    def test_a_daily_duty_or_a_stated_start_asks_nothing(self):
        self.propose("Stretch #anywhere 🔁 every day")
        self.assertEqual(self.controls()[0], "Approve")
        self.agent.reject(self.pending()[0]["id"])
        self.propose("Pay the club dues #computer 🔁 every month on the 15th 🛫 2026-10-10 📅 2026-10-15")
        self.assertEqual(self.controls()[0], "Approve")


if __name__ == "__main__":
    unittest.main()
