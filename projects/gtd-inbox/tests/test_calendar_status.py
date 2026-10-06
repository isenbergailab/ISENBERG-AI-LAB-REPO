"""Item 12: project status from the calendar. An event belongs to a project when its title or notes name the
project's key or an alias. Current state shows the next one; once one ends, APPROVAL asks what came out of it."""
import json
import os
from datetime import datetime
from unittest import mock

from helpers import VaultCase
from gtd_agent.housekeeping import Housekeeper

NOW = datetime(2026, 9, 28, 9, 0)
PLANTS = "02_PROJECTS/PLANTS/PLANTS.md"
LAB = "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"
PLANTS_TEXT = """---
key: PLANTS
type: project
status: active
area: "[[AREA_CLUB]]"
aliases: [Houseplants]
private: false
---
# PLANTS

## Outcome
Healthy plants in every room.

## Current state
_The GTD agent keeps this current._

## Steps
- [ ] Buy pots #errands #next

## Log
- 2026-09-27 Picked the rooms.
"""


def event(uid: str, summary: str, start: str, end: str, notes: str = "") -> str:
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART:{start}\r\nDTEND:{end}\r\nSUMMARY:{summary}\r\n"
            + (f"DESCRIPTION:{notes}\r\n" if notes else "") + "END:VEVENT\r\n")


CALENDAR = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            + event("old", "Demo Project planning", "20260927T100000", "20260927T110000")  # before this started
            + event("kick", "Demo Project kickoff", "20260928T080000", "20260928T083000")
            + event("tax", "Taxes call", "20260928T070000", "20260928T073000")
            + event("walk", "Walkthrough", "20261006T150000", "20261006T160000", "Agenda for the houseplants")
            + event("swap", "Houseplants swap", "20261010T100000", "20261010T120000")
            + event("dentist", "Dentist", "20261001T090000", "20261001T100000")
            + "END:VCALENDAR\r\n")


class CalendarStatusCase(VaultCase):
    remote = True
    calendar = CALENDAR

    def extra_config(self):
        return '[calendar]\nenabled = true\nsource = "ics"\n'

    def extra_files(self):
        return {PLANTS: PLANTS_TEXT}

    def setUp(self):
        link = mock.patch.dict(os.environ, {"GTD_CALENDAR_ICS_URL": "https://calendar.example/private.ics"})
        link.start()
        self.addCleanup(link.stop)
        super().setUp()
        cache = self.settings.state_dir / "calendar" / "cache.ics"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(self.calendar, encoding="utf-8")  # fresh, so nothing is fetched

    def keep(self, now: datetime = NOW) -> list[str]:
        return Housekeeper(self.agent).run(now=now)

    def pending_kind(self, kind: str) -> list:
        return [r for r in self.agent.ledger.pending() if json.loads(r["proposal_json"])["kind"] == kind]

    def answer(self, row_id: str, text: str) -> list[str]:
        approval = self.read("_agent/APPROVAL.md")
        start = approval.index(f"<!-- gtd-agent:proposal id={row_id} -->")
        end = approval.index("<!-- gtd-agent:item-end -->", start)
        block = approval[start:end].replace("> Write one line, then tick Save.", f"> {text}")
        self.write("_agent/APPROVAL.md", approval[:start] + block.replace("- [ ] Save", "- [x] Save", 1)
                   + approval[end:])
        return self.agent.sync_review_requests()


class TodayCalendarTests(CalendarStatusCase):
    calendar = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
                + event("retreat", "Retreat", "20260927T200000", "20260928T090000")       # began yesterday
                + event("conf", "Conference", "20260928T090000", "20260929T170000")       # runs into tomorrow
                + event("tour", "Lab tour", "20260928T150000", "20260928T160000") * 2     # listed twice
                + "END:VCALENDAR\r\n")

    def test_today_shows_each_event_once_with_its_real_span(self):
        Housekeeper(self.agent).digest(self.agent.vault(), now=NOW)
        line = next(l for l in self.read("_agent/TODAY.md").splitlines() if l.startswith("- Calendar:"))
        self.assertEqual(line, "- Calendar: until 09:00 Retreat · 09:00 until Tue 17:00 Conference · "
                               "15:00–16:00 Lab tour")


class NextEventTests(CalendarStatusCase):
    def test_current_state_shows_the_next_event(self):
        self.keep()
        section = self.read(PLANTS).split("## Current state\n", 1)[1].split("\n\n## ", 1)[0].strip()
        self.assertEqual(section, "- Next: Buy pots\n"
                                  "- Next event: 2026-10-06 15:00 Walkthrough\n"  # its notes name the alias
                                  "- Last change: 2026-09-27 Picked the rooms.")


class AfterEventTests(CalendarStatusCase):
    def test_one_question_per_ended_event(self):
        self.keep()
        self.keep(datetime(2026, 9, 28, 9, 30))
        titles = sorted(json.loads(r["proposal_json"])["title"] for r in self.pending_kind("after_event"))
        self.assertEqual(titles, ["Demo Project kickoff", "Taxes call"])  # not before this started, not ahead, not unnamed
        approval = self.read("_agent/APPROVAL.md")
        self.assertIn("## What came out of it?", approval)
        self.assertIn("**What came out of it:**", approval)

    def test_a_saved_answer_is_a_log_line_then_step_proposals(self):
        self.keep()
        row = next(r for r in self.pending_kind("after_event") if "Demo Project" in r["proposal_json"])
        self.answer(row["id"], "Advisor agreed to sponsor; budget draft due Friday")
        self.assertIn("- 2026-09-28 Demo Project kickoff: Advisor agreed to sponsor; budget draft due Friday",
                      self.read(LAB))
        self.provider.after_event_answers.append(
            {"steps": [{"title": "Draft the lab budget", "context": "#computer"}], "explanation": "The budget is next."})
        self.keep(datetime(2026, 9, 28, 9, 30))
        call = next(c for name, c in self.provider.calls if name == "after_event")
        self.assertEqual((call["project"], call["outcome"]),
                         ("DEMO_PROJECT", "Advisor agreed to sponsor; budget draft due Friday"))
        [steps] = self.pending_kind("after_event_steps")
        self.assertEqual([item["label"] for item in json.loads(steps["proposal_json"])["items"]],
                         ["Step · Draft the lab budget → Step queued in DEMO_PROJECT › Steps (gets #next when its "
                          "turn comes)"])
        self.keep(datetime(2026, 9, 28, 10, 0))
        self.assertEqual(len([name for name, _ in self.provider.calls if name == "after_event"]), 1)

    def test_a_private_project_gets_its_log_line_and_no_model(self):
        self.keep()
        row = next(r for r in self.pending_kind("after_event") if "Taxes" in r["proposal_json"])
        self.answer(row["id"], "Accountant needs the 1099s")
        self.assertIn("- 2026-09-28 Taxes call: Accountant needs the 1099s", self.read("02_PROJECTS/TAXES/TAXES.md"))
        self.keep(datetime(2026, 9, 28, 9, 30))
        self.assertEqual([name for name, _ in self.provider.calls if name == "after_event"], [])
