"""Proton Calendar: the agent reads it through the share link and cannot write to it, so an event it finds becomes
an action to add it by hand, carrying the day, time and place."""
from helpers import VaultCase, draft

SINGLE = "01_GTD/SINGLE_ACTIONS.md"


class ProtonCase(VaultCase):
    def extra_config(self):
        return '[calendar]\nenabled = true\nsource = "ics"\n'

    def capture(self, line):
        self.add_inbox(line)
        return self.agent.scan().new_proposals[0]["proposal"]


class EventCaptureTests(ProtonCase):
    def test_an_event_capture_becomes_an_action_to_add_it_by_hand(self):
        proposal = self.capture("Advisor meeting #event 2026-10-06 15:30-16:15")
        self.assertEqual(proposal["title"], "Add to Proton Calendar: Advisor meeting, Tue Oct 6 15:30–16:15")
        self.tick()
        self.agent.sync_review_requests()
        line = next(line for line in self.read(SINGLE).splitlines() if "Advisor meeting" in line)
        self.assertTrue(line.startswith("- [ ] Add to Proton Calendar: Advisor meeting, Tue Oct 6 15:30–16:15 "
                                        "#computer 📅 2026-09-28"), line)
        self.assertNotIn("Advisor meeting", self.read("00_INBOX/INBOX.md"))

    def test_a_past_day_is_still_refused(self):
        proposal = self.capture("Advisor meeting #event 2026-09-01 15:30")
        self.assertEqual(proposal["kind"], "manual")
        self.assertIn("That day has passed", proposal["reason"])


class ModelEventTests(ProtonCase):
    remote = True

    def test_a_meeting_in_plain_words_becomes_an_action_to_add_it(self):
        self.add_inbox("meet Nora on Tuesday at 3pm for an hour about the sponsorship")
        self.provider.decisions.append({"operation": ("new_capture", 0.9), "category": ("calendar", 0.9),
                                        "multiplicity": ("single", 0.9)})
        self.provider.interpretations.append(draft(route="calendar_event", title="Meet Nora about the sponsorship",
                                                   due_evidence="Tuesday", time_evidence="3pm",
                                                   end_evidence="for an hour"))
        proposal = self.agent.scan().new_proposals[0]["proposal"]
        self.assertEqual(proposal["title"],
                         "Add to Proton Calendar: Meet Nora about the sponsorship, Tue Sep 29 15:00–16:00")
