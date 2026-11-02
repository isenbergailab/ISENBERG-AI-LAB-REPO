"""A calendar birthday becomes a reviewed, local reminder."""
from datetime import datetime, timedelta

from helpers import VaultCase
from gtd_agent.birthdays import BirthdayImporter
from gtd_agent.calendar_ics import Event


def event(name: str, day: str, uid: str, private: bool = False) -> Event:
    start = datetime.fromisoformat(day + "T09:00:00")
    return Event(name, start, start + timedelta(hours=1), False, False, uid, private=private)


class BirthdayImportTests(VaultCase):
    def test_calendar_birthdays_are_reviewed_before_tasks_are_added(self):
        events = [
            event("Avery North Birthday", "2026-02-20", "avery"),
            event("Avery North Birthday", "2027-02-20", "avery"),
            event("Blake West Bday", "2026-07-10", "blake-old"),
            event("Blake West Bday", "2027-07-10", "blake-new"),
            event("Ask parent what they want for bday", "2026-10-05", "errand"),
            event("Pat Bday Party 93rd", "2026-06-27", "party"),
            event("Private Person Birthday", "2027-02-11", "private", private=True),
        ]
        importer = BirthdayImporter(self.agent, reader=lambda start, end: (events, None))
        result = importer.scan()
        self.assertEqual(result.count, 2)
        review = self.read("_agent/APPROVAL.md")
        self.assertIn("Change: Annual message tasks in [[SINGLE_ACTIONS]] › Birthdays.", review)
        self.assertIn("Avery North · Feb 20", review)
        self.assertIn("Blake West · Jul 10", review)
        self.assertNotIn("Private Person", review)
        self.assertNotIn("Ask parent", review)
        self.assertNotIn("Pat Bday Party", review)
        self.assertEqual(self.provider.calls, [])
        self.assertNotIn("Message Avery North", self.read("01_GTD/SINGLE_ACTIONS.md"))
        self.tick("Approve", result.proposal_id)
        self.assertIn("Applied birthday_import", self.agent.sync_review_requests()[0])
        saved = self.read("01_GTD/SINGLE_ACTIONS.md")
        self.assertIn("## Birthdays", saved)
        self.assertIn("Message Avery North on birthday #anywhere 🔁 every year 🛫 2027-02-20 📅 2027-02-20", saved)
        self.assertEqual(saved.count("Message Blake West on birthday"), 1)
        self.assertNotIn("## Birthdays", self.read("_agent/APPROVAL.md"))

    def test_ambiguous_dates_are_flagged_without_guessing(self):
        events = [
            event("Remy Lane Birthday", "2027-01-08", "remy-wrong"),
            event("Remy's Birthday", "2027-02-08", "remy-correct"),
            event("Mira Vale Birthday", "2026-12-05", "mira"),
            event("Mira Vale Birthday", "2026-12-12", "mira"),
        ]
        importer = BirthdayImporter(self.agent, reader=lambda start, end: (events, None))
        result = importer.scan(preferred_dates={"remy": "02-08"})
        self.assertEqual(result.count, 1)
        self.assertEqual(result.flagged, ["Mira Vale: calendar repeats on multiple dates"])
        review = self.read("_agent/APPROVAL.md")
        self.assertIn("Remy · Feb 8", review)
        self.assertNotIn("Jan 8", review)
        self.assertIn("Mira Vale", review.split("<details>", 1)[0])
        self.tick("Approve", result.proposal_id)
        self.agent.sync_review_requests()
        saved = self.read("01_GTD/SINGLE_ACTIONS.md")
        self.assertIn("Message Remy on birthday", saved)
        self.assertNotIn("Message Mira Vale", saved)

    def test_person_can_be_unticked_before_approval(self):
        events = [event("Avery North Birthday", "2027-02-20", "avery"),
                  event("Blake West Bday", "2027-07-10", "blake")]
        result = BirthdayImporter(self.agent, reader=lambda start, end: (events, None)).scan()
        review = self.read("_agent/APPROVAL.md")
        self.write("_agent/APPROVAL.md", review.replace("- [x] 2. Blake West", "- [ ] 2. Blake West"))
        self.tick("Approve", result.proposal_id)
        self.agent.sync_review_requests()
        saved = self.read("01_GTD/SINGLE_ACTIONS.md")
        self.assertIn("Message Avery North", saved)
        self.assertNotIn("Message Blake West", saved)

    def test_same_scan_does_not_create_another_proposal(self):
        events = [event("Avery North Birthday", "2027-02-20", "avery")]
        importer = BirthdayImporter(self.agent, reader=lambda start, end: (events, None))
        first = importer.scan()
        second = importer.scan()
        self.assertEqual(first.proposal_id, second.proposal_id)
        self.assertEqual(len(self.agent.ledger.pending()), 1)
        self.tick("Approve", first.proposal_id)
        self.agent.sync_review_requests()
        third = importer.scan()
        self.assertEqual(third.count, 0)
        self.assertIsNone(third.proposal_id)

    def test_existing_birthday_task_in_an_area_is_not_duplicated(self):
        area = self.read("03_AREAS/AREA_CLUB.md")
        area += ("\n## Recurring\n- [ ] Casey Hart Bday #anywhere #next 🔁 every year "
                 "🛫 2027-03-01 📅 2027-03-01\n")
        self.write("03_AREAS/AREA_CLUB.md", area)
        events = [event("Casey Hart Bday", "2027-03-01", "casey")]
        result = BirthdayImporter(self.agent, reader=lambda start, end: (events, None)).scan()
        self.assertEqual(result.count, 0)
        self.assertIsNone(result.proposal_id)

    def test_calendar_failure_never_changes_approvals(self):
        importer = BirthdayImporter(self.agent, reader=lambda start, end: (None, "Calendar unavailable"))
        result = importer.scan()
        self.assertEqual(result.warning, "Calendar unavailable")
        self.assertEqual(self.agent.ledger.pending(), [])
