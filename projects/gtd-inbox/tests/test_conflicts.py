"""A new event that overlaps something on the calendar gets a warning (phase 3 item 2).

The agent reads the calendar through the Proton share link, so it can only warn: the event itself is still an
action SampleOwner adds by hand. The warning is written into the proposal when it is made, so what he sees in APPROVAL
does not change under his tick.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from unittest import mock

import test_mail
from helpers import VaultCase
from gtd_agent.calendar_ics import Event, overlapping
from gtd_agent.housekeeping import Housekeeper
from gtd_agent.workflow import Agent
from test_mail import mail_answer, message

LAB = "DEMO_ROOM 106 Lab"


def event(uid: str, summary: str, start: str, end: str, extra: str = "") -> str:
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART{extra}:{start}\r\nDTEND{extra}:{end}\r\nSUMMARY:{summary}\r\n"
            "END:VEVENT\r\n")


def calendar(*events: str) -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(events) + "END:VCALENDAR\r\n"


CLASS_DAY = calendar(
    event("lab", LAB, "20261006T150000", "20261006T161500"),
    event("coffee", "Coffee", "20261006T140000", "20261006T153000"),  # ends the minute the meeting starts
)


class CalendarCase(VaultCase):
    ics = CLASS_DAY

    def extra_config(self) -> str:
        return '[calendar]\nenabled = true\nsource = "ics"\n'

    def setUp(self) -> None:
        link = mock.patch.dict(os.environ, {"GTD_CALENDAR_ICS_URL": "https://calendar.example/private.ics"})
        link.start()
        self.addCleanup(link.stop)
        super().setUp()
        cache = self.settings.state_dir / "calendar" / "cache.ics"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(self.ics, encoding="utf-8")  # fresh, so nothing is fetched

    def capture(self, line: str) -> dict:
        self.add_inbox(line)
        return self.agent.scan().new_proposals[0]["proposal"]


class InboxEventTests(CalendarCase):
    def test_an_event_that_overlaps_a_class_is_flagged_in_approval(self):
        proposal = self.capture("Advisor meeting #event 2026-10-06 15:30-16:15")
        self.assertEqual(proposal["conflicts"], [f"Conflict: overlaps {LAB} (15:00–16:15)"])
        self.assertIn(f"Conflict: overlaps {LAB} (15:00–16:15)", self.read("_agent/APPROVAL.md"))

    def test_back_to_back_is_not_a_conflict(self):
        proposal = self.capture("Advisor meeting #event 2026-10-06 16:15-17:00")  # starts as the lab ends
        self.assertNotIn("conflicts", proposal)

    def test_another_day_is_clear(self):
        proposal = self.capture("Advisor meeting #event 2026-10-07 15:30-16:15")
        self.assertNotIn("conflicts", proposal)
        self.assertNotIn("Conflict", self.read("_agent/APPROVAL.md"))

    def test_several_overlaps_are_one_warning(self):
        proposal = self.capture("Long review #event 2026-10-06 13:00-17:00")
        self.assertEqual(proposal["conflicts"], [f"Conflict: overlaps Coffee (14:00–15:30), {LAB} (15:00–16:15)"])

    def test_an_event_already_on_the_calendar_is_called_that_not_a_conflict(self):
        proposal = self.capture(f"{LAB} #event 2026-10-06 15:00-16:15")
        self.assertEqual(proposal["conflicts"], [f"Already on your calendar: {LAB} (15:00–16:15)"])

    def test_the_proposal_still_adds_the_event_action(self):
        self.capture("Advisor meeting #event 2026-10-06 15:30-16:15")
        self.tick()
        self.agent.sync_review_requests()
        self.assertIn("Add to Proton Calendar: Advisor meeting, Tue Oct 6 15:30–16:15", self.read("01_GTD/SINGLE_ACTIONS.md"))

    def test_an_action_that_is_not_an_event_never_reads_the_calendar(self):
        with mock.patch.object(Agent, "calendar_events", side_effect=AssertionError("read the calendar")):
            proposal = self.capture("Renew the library card #computer")
        self.assertNotIn("conflicts", proposal)


class OverlapTests(CalendarCase):
    def test_events_that_only_touch_do_not_overlap(self):
        def at(start: str, end: str, **kw) -> Event:
            fields = dict(summary="x", start=datetime.fromisoformat(start), end=datetime.fromisoformat(end),
                          all_day=False, busy=True)
            return Event(**{**fields, **kw})
        window = (datetime(2026, 10, 6, 15, 30), datetime(2026, 10, 6, 16, 15))
        ends_as_it_starts = at("2026-10-06T14:00", "2026-10-06T15:30")
        starts_as_it_ends = at("2026-10-06T16:15", "2026-10-06T17:00")
        inside = at("2026-10-06T15:45", "2026-10-06T16:00")
        around = at("2026-10-06T09:00", "2026-10-06T20:00")
        all_day = at("2026-10-06T00:00", "2026-10-07T00:00", all_day=True, busy=True)
        free = at("2026-10-06T15:30", "2026-10-06T16:15", busy=False)
        self.assertEqual(overlapping([ends_as_it_starts, starts_as_it_ends, inside, around, all_day, free], *window),
                         [inside, around])

    def test_a_proposal_housekeeping_makes_is_checked_too(self):
        op = {"op": "calendar_add", "path": "calendar", "calendar": "Cal", "uid": "gtd-x", "summary": "Advisor meeting",
              "start": "2026-10-06T15:30", "end": "2026-10-06T16:15", "alert_minutes": 15, "rrule": None, "when": "x"}
        Housekeeper(self.agent)._propose("calendar", "k", {"kind": "calendar_event", "title": "Advisor meeting",
                                                           "summary": "x", "reason": "y", "ops": [op]})
        proposal = json.loads(self.agent.ledger.get_by_key("k")["proposal_json"])
        self.assertEqual(proposal["conflicts"], [f"Conflict: overlaps {LAB} (15:00–16:15)"])


class FreeTimeTests(CalendarCase):
    ics = calendar(
        event("birthday", "Mariska Birthday", "20261006", "20261007", ";VALUE=DATE"),  # all day
        "BEGIN:VEVENT\r\nUID:free\r\nDTSTART:20261006T153000\r\nDTEND:20261006T160000\r\nSUMMARY:Maybe lunch\r\n"
        "TRANSP:TRANSPARENT\r\nEND:VEVENT\r\n",
        event("marker", "Interview ends", "20261006T154500", "20261006T154500"),  # a point in time
        "BEGIN:VEVENT\r\nUID:gone\r\nDTSTART:20261006T150000\r\nDTEND:20261006T160000\r\nSUMMARY:Cancelled call\r\n"
        "STATUS:CANCELLED\r\nEND:VEVENT\r\n",
    )

    def test_all_day_free_cancelled_and_zero_length_events_do_not_conflict(self):
        proposal = self.capture("Advisor meeting #event 2026-10-06 15:30-16:15")
        self.assertNotIn("conflicts", proposal)


class CalendarOffTests(VaultCase):
    def test_no_calendar_means_no_claim_either_way(self):
        self.add_inbox("Advisor meeting #event 2026-10-06 15:30-16:15")
        proposal = self.agent.scan().new_proposals[0]["proposal"]
        self.assertNotIn("conflicts", proposal)


class MailEventTests(test_mail.LabeledMailCase):
    def extra_config(self) -> str:
        return super().extra_config() + '[calendar]\nenabled = true\nsource = "ics"\n'

    def setUp(self) -> None:
        link = mock.patch.dict(os.environ, {"GTD_CALENDAR_ICS_URL": "https://calendar.example/private.ics"})
        link.start()
        self.addCleanup(link.stop)
        super().setUp()
        cache = self.settings.state_dir / "calendar" / "cache.ics"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(CLASS_DAY, encoding="utf-8")

    def test_an_event_in_mail_is_flagged_on_its_own_box(self):
        self.add(message("Pat Lee <pat@example.net>", "Kickoff", body="Kickoff is Tuesday Oct 6 at 3:30pm."))
        self.provider.mails.append(mail_answer(events=[
            {"title": "Lab kickoff", "day_evidence": "Oct 6", "time_evidence": "3:30pm", "end_evidence": "",
             "place": "", "evidence": "Kickoff is Tuesday Oct 6 at 3:30pm"}]))
        Housekeeper(self.agent).run(now=datetime(2026, 9, 30, 9, 0))
        proposal = json.loads(self.pending("mail")[0]["proposal_json"])
        label = proposal["items"][0]["label"]
        self.assertIn("Event · Add to Proton Calendar: Lab kickoff, Tue Oct 6 15:30–16:30", label)
        self.assertIn(f"Conflict: overlaps {LAB} (15:00–16:15)", label)
        self.assertNotIn("conflicts", proposal)  # one warning, on the box it belongs to
