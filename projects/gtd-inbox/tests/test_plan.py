"""The day's plan (phase 3 item 3): the top tasks placed in free gaps, a file of time blocks for Proton Calendar,
and the week's load.

The agent reads the calendar through the Proton share link and cannot write to it, so the blocks are an .ics file
SampleOwner imports himself. Nothing here reaches a model: the plan is rules over his own vault and calendar.
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from helpers import TODAY, VaultCase, make_settings, make_vault
from test_conflicts import calendar, event
from gtd_agent.calendar_ics import Event, events_ics, free_gaps, overlapping, parse_events
from gtd_agent.cli import main
from gtd_agent.housekeeping import Housekeeper
from gtd_agent.planner import Planner, Want, place
from gtd_agent.tasks import parse_task_line
from gtd_agent.workflow import Agent


def at(hour: int, minute: int = 0, day: int = 28) -> datetime:
    return datetime(2026, 9, day, hour, minute)


class SettingsTests(unittest.TestCase):
    def settings(self, extra: str = ""):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        make_vault(base / "vault")
        return make_settings(base, extra=extra)

    def test_by_default_the_plan_holds_three_tasks_on_every_day(self):
        s = self.settings()
        self.assertEqual((s.plan_tasks, s.plan_default_minutes, s.plan_max_block_minutes, s.plan_hour), (3, 30, 120, 6))
        self.assertEqual(s.plan_days, (0, 1, 2, 3, 4, 5, 6))
        self.assertEqual(s.blocks_file, "_agent/time-blocks.ics")

    def test_the_plan_table_sets_each_value(self):
        s = self.settings('[plan]\ntasks = 2\ndefault_minutes = 45\nmax_block_minutes = 90\nhour = 7\n'
                          'days = ["mon", "wed", "Friday"]\n')
        self.assertEqual((s.plan_tasks, s.plan_default_minutes, s.plan_max_block_minutes, s.plan_hour), (2, 45, 90, 7))
        self.assertEqual(s.plan_days, (0, 2, 4))

    def test_values_that_cannot_work_are_refused(self):
        for bad in ("tasks = 0", "tasks = 11", "default_minutes = 0", "max_block_minutes = 10", "hour = 24",
                    "days = []", 'days = ["funday"]'):
            with self.subTest(bad), self.assertRaises(ValueError):
                self.settings(f"[plan]\n{bad}\n")


class BlocksFileTests(unittest.TestCase):
    """One calendar file holds every block, so SampleOwner imports once."""
    STAMP = datetime(2026, 9, 28, 13, 0)

    def make(self) -> str:
        return events_ics([
            ("gtd-block-20260928-aaa", "Do: Pay parking ticket", at(10), at(10, 30)),
            ("gtd-block-20260928-bbb", "Do: Draft the budget; 1, 2", at(13), at(15)),
        ], zone="America/New_York", alert_minutes=10, stamp=self.STAMP)

    def test_one_calendar_holds_every_block_with_its_alert(self):
        text = self.make()
        self.assertEqual(text.count("BEGIN:VCALENDAR"), 1)
        self.assertEqual(text.count("BEGIN:VTIMEZONE"), 1)
        self.assertEqual(text.count("BEGIN:VEVENT"), 2)
        self.assertEqual(text.count("TRIGGER:-PT10M"), 2)
        self.assertIn("DTSTART;TZID=America/New_York:20260928T100000\r\n", text)
        self.assertIn("DTEND;TZID=America/New_York:20260928T150000\r\n", text)
        self.assertIn("UID:gtd-block-20260928-aaa\r\n", text)
        self.assertIn("SUMMARY:Do: Draft the budget\\; 1\\, 2\r\n", text)
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))

    def test_the_file_reads_back_as_the_same_blocks(self):
        events = parse_events(self.make(), at(0), at(0, 0, 29))
        self.assertEqual([(e.summary, e.start, e.end, e.busy, e.uid) for e in events], [
            ("Do: Pay parking ticket", at(10), at(10, 30), True, "gtd-block-20260928-aaa"),
            ("Do: Draft the budget; 1, 2", at(13), at(15), True, "gtd-block-20260928-bbb")])

    def test_a_point_in_time_does_not_split_free_time(self):
        marker = Event("Interview ends", at(10), at(10), False, True)
        self.assertEqual(free_gaps([marker], date(2026, 9, 28), (9, 0), (12, 0), None, 15), [(at(9), at(12))])


def want(text: str, default: int = 30) -> Want:
    task = parse_task_line(f"- [ ] {text}", "a.md", 1)
    return Want(task, task.minutes or default, task.minutes is None)


class PlaceTests(unittest.TestCase):
    """Tasks go into free gaps in rank order, each into the earliest gap that holds it."""

    def test_each_task_takes_the_earliest_gap_that_fits_and_the_gap_shrinks(self):
        wants = [want("Big #2h"), want("Small #15m"), want("Medium #30m")]
        placed, left = place(wants, [(at(10), at(12)), (at(13), at(15))], limit=3, max_block=120)
        self.assertEqual([(p.want.task.title, p.start, p.end) for p in placed], [
            ("Big", at(10), at(12)), ("Small", at(13), at(13, 15)), ("Medium", at(13, 15), at(13, 45))])
        self.assertEqual(left, [])

    def test_a_start_is_rounded_up_to_the_quarter_hour(self):
        placed, _ = place([want("Call #30m")], [(at(9, 7), at(10))], 3, 120)
        self.assertEqual((placed[0].start, placed[0].end), (at(9, 15), at(9, 45)))

    def test_a_gap_that_is_too_small_once_rounded_is_skipped(self):
        placed, _ = place([want("Call #30m")], [(at(9, 7), at(9, 40)), (at(11), at(12))], 3, 120)
        self.assertEqual(placed[0].start, at(11))

    def test_a_long_task_gets_a_capped_block_and_is_marked_partial(self):
        placed, _ = place([want("Write #4h")], [(at(9), at(18))], 3, 120)
        self.assertEqual((placed[0].end - placed[0].start, placed[0].partial), (timedelta(hours=2), True))
        whole, _ = place([want("Write #2h")], [(at(9), at(18))], 3, 120)
        self.assertFalse(whole[0].partial)

    def test_what_fits_nowhere_is_left_over_and_the_next_task_still_goes_in(self):
        placed, left = place([want("Write #2h"), want("Call #15m")], [(at(9), at(9, 45))], 3, 120)
        self.assertEqual([p.want.task.title for p in placed], ["Call"])
        self.assertEqual([w.task.title for w in left], ["Write"])

    def test_it_stops_at_the_limit(self):
        placed, left = place([want(f"Task {n} #15m") for n in range(5)], [(at(9), at(18))], 3, 120)
        self.assertEqual([p.want.task.title for p in placed], ["Task 0", "Task 1", "Task 2"])
        self.assertEqual(left, [])  # the rest were never tried


NOW = datetime(2026, 9, 28, 9, 0)  # Monday: the fixture's today
SINGLE = "01_GTD/SINGLE_ACTIONS.md"
LAB = "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"
TAXES = "02_PROJECTS/TAXES/TAXES.md"
LATIN = "02_PROJECTS/LATIN/LATIN.md"
BLOCKS = "_agent/time-blocks.ics"

SINGLE_TEXT = """# SINGLE_ACTIONS

## @computer
- [ ] Renew library card #computer #15m 📅 2026-09-28
- [ ] Pay parking ticket #computer 📅 2026-09-25

## @calls
- [ ] Call the dentist [[AREA_ADMIN]] #calls #15m

## @anywhere

## @errands

## Waiting For
- [ ] #waiting Riley: signed form [[AREA_ADMIN]] ➕ 2026-09-15 📅 2026-09-25
"""
LAB_TEXT = """---
key: DEMO_PROJECT
type: project
status: active
area: "[[AREA_CLUB]]"
due: 2026-12-01
priority: high
---
# DEMO_PROJECT

## Steps
- [ ] Email the advisor #computer #next #30m
- [ ] Draft the budget #computer #next #2h 📅 2026-09-30
- [ ] Print the flyers #errands #next 🛫 2026-10-02
- [ ] Book the room #calls

## Waiting For
- [ ] #waiting Admin office: room approval ➕ 2026-09-20 📅 2026-09-26
"""
TAXES_TEXT = """---
key: TAXES
type: project
status: active
area: "[[AREA_ADMIN]]"
---
# TAXES

## Steps
- [ ] Download 1099 forms #computer #next #15m
"""
DAY = calendar(
    event("standup", "Stand-up", "20260928T090000", "20260928T100000"),
    event("lunch", "Lunch", "20260928T120000", "20260928T130000"),
    event("dentist", "Dentist", "20260928T150000", "20260928T160000"),
)
# By rank: Pay parking ticket (overdue), Renew library card (due today), Draft the budget (due Wednesday). The
# free gaps from nine o'clock are 10:00-12:00, 13:00-15:00 and 16:00-22:00.
STANDARD = [
    "- Plan for today:",
    "  - 10:00–10:30 Pay parking ticket · 30m guessed · 3 days overdue · [[SINGLE_ACTIONS]]",
    "  - 10:30–10:45 Renew library card · 15m · due today · [[SINGLE_ACTIONS]]",
    "  - 13:00–15:00 Draft the budget · 2h · due Wed Sep 30 · [[DEMO_PROJECT]]",
    "  - Calendar file: `_agent/time-blocks.ics`. Import it into Proton Calendar.",
]


def plan_of(today_note: str) -> list[str]:
    """The plan as TODAY shows it: its heading line, then the lines under it. Empty when TODAY has none."""
    lines = today_note.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(("- Plan for today", "- Top "))), None)
    if start is None:
        return []
    block = [lines[start]]
    for line in lines[start + 1:]:
        if not line.startswith("  - "):
            break
        block.append(line)
    return block


class PlanCase(VaultCase):
    ics = DAY
    plan_config = ""

    def extra_config(self) -> str:
        return '[calendar]\nenabled = true\nsource = "ics"\n' + self.plan_config

    def extra_files(self) -> dict[str, str]:
        return {SINGLE: SINGLE_TEXT, LAB: LAB_TEXT, TAXES: TAXES_TEXT}

    def setUp(self) -> None:
        link = mock.patch.dict(os.environ, {"GTD_CALENDAR_ICS_URL": "https://calendar.example/private.ics"})
        link.start()
        self.addCleanup(link.stop)
        super().setUp()
        self.calendar(self.ics)

    def calendar(self, text: str) -> None:
        cache = self.settings.state_dir / "calendar" / "cache.ics"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")  # fresh, so nothing is fetched

    def keep(self, now: datetime = NOW) -> list[str]:
        """One check of the vault. The same Housekeeper each time, as in the running agent."""
        if not hasattr(self, "house"):
            self.house = Housekeeper(self.agent)
        return self.house.run(now=now)

    def plan(self) -> list[str]:
        block = plan_of(self.read("_agent/TODAY.md"))
        self.assertTrue(block, "TODAY has no plan")
        return block

    def has_plan(self) -> bool:
        return bool(plan_of(self.read("_agent/TODAY.md")))

    def blocks(self) -> list[Event]:
        return parse_events(self.path(BLOCKS).read_text(encoding="utf-8"), at(0), at(0, 0, 29))


class PlanTodayTests(PlanCase):
    def test_the_top_three_go_into_free_gaps_in_rank_order(self):
        self.keep()
        self.assertEqual(self.plan(), STANDARD)

    def test_the_plan_is_made_once_and_stays_when_the_vault_changes(self):
        self.keep()
        self.write(SINGLE, self.read(SINGLE).replace("## @calls", "- [ ] Mail the rent check #computer 📅 2026-09-20\n\n## @calls"))
        self.keep(NOW + timedelta(minutes=5))
        self.assertEqual(self.plan(), STANDARD)

    def test_a_new_plan_on_request_starts_from_now_and_takes_the_new_top_tasks(self):
        self.keep()
        self.write(SINGLE, self.read(SINGLE).replace("## @calls", "- [ ] Mail the rent check #computer 📅 2026-09-20\n\n## @calls"))
        Planner(self.agent).redo(self.agent.vault(), NOW)
        self.keep(NOW + timedelta(minutes=5))
        self.assertEqual(self.plan()[1], "  - 10:00–10:30 Mail the rent check · 30m guessed · 8 days overdue · [[SINGLE_ACTIONS]]")

    def test_the_plan_does_not_exist_before_its_hour(self):
        self.keep(datetime(2026, 9, 28, 5, 30))
        self.assertFalse(self.has_plan())
        self.assertFalse(self.path(BLOCKS).exists())
        self.keep(datetime(2026, 9, 28, 6, 0))
        self.assertTrue(self.has_plan())


class OtherDaysTests(PlanCase):
    plan_config = '[plan]\ndays = ["tue", "wed"]\n'

    def test_a_day_left_out_of_the_plan_days_has_no_plan(self):
        self.keep()  # Monday
        self.assertFalse(self.has_plan())
        self.assertFalse(self.path(BLOCKS).exists())


class DefaultMinutesTests(PlanCase):
    plan_config = "[plan]\ndefault_minutes = 45\n"

    def test_tasks_with_no_time_tag_count_for_the_default_minutes(self):
        self.keep()
        self.assertEqual(self.plan()[1],
                         "  - 10:00–10:45 Pay parking ticket · 45m guessed · 3 days overdue · [[SINGLE_ACTIONS]]")


class TwoTasksTests(PlanCase):
    plan_config = "[plan]\ntasks = 2\n"

    def test_the_task_count_is_a_setting(self):
        self.keep()
        self.assertEqual(self.plan(), [STANDARD[0], STANDARD[1], STANDARD[2], STANDARD[4]])


class FileTests(PlanCase):
    def test_the_blocks_are_one_calendar_file_with_an_alert_each(self):
        self.keep()
        text = self.path(BLOCKS).read_bytes().decode("utf-8")
        self.assertEqual([(e.summary, e.start, e.end) for e in self.blocks()], [
            ("Do: Pay parking ticket", at(10), at(10, 30)),
            ("Do: Renew library card", at(10, 30), at(10, 45)),
            ("Do: Draft the budget", at(13), at(15))])
        self.assertEqual(text.count("BEGIN:VALARM"), 3)
        self.assertNotIn("\n", text.replace("\r\n", ""))  # calendar files end their lines with CRLF

    def test_the_vault_check_leaves_the_file_alone(self):
        self.keep()
        self.assertTrue(self.path(BLOCKS).exists())
        self.assertNotIn("time-blocks", self.read("_agent/LINT.md"))
        self.assertNotIn("time-blocks", self.read("_agent/APPROVAL.md"))

    def test_the_blocks_keep_clear_of_the_calendar(self):
        self.keep()
        busy = parse_events(DAY, at(0), at(0, 0, 29))
        for block in self.blocks():
            self.assertEqual(overlapping(busy, block.start, block.end), [], block.summary)

    def test_a_task_keeps_its_uid_when_the_plan_is_made_again(self):
        self.keep()
        first = {e.summary: e.uid for e in self.blocks()}
        self.write(SINGLE, self.read(SINGLE).replace("## @calls", "- [ ] Mail the rent check #computer 📅 2026-09-20\n\n## @calls"))
        redone = Planner(self.agent).redo(self.agent.vault(), NOW)
        again = {e.summary: e.uid for e in self.blocks()}
        self.assertEqual(sorted(again), ["Do: Mail the rent check", "Do: Pay parking ticket", "Do: Renew library card"])
        self.assertEqual(again["Do: Pay parking ticket"], first["Do: Pay parking ticket"])  # Proton replaces, not doubles
        self.assertEqual(again["Do: Renew library card"], first["Do: Renew library card"])
        self.assertEqual(len(set(again.values())), 3)
        self.assertEqual(Planner(self.agent).saved(TODAY), redone)

    def test_a_day_with_no_gap_has_no_file_and_says_so(self):
        self.calendar(calendar(event("conf", "Conference", "20260928T080000", "20260928T220000")))
        self.keep()
        self.assertFalse(self.path(BLOCKS).exists())
        self.assertEqual(self.plan(), [
            "- Plan for today: no free gap fits the top tasks.",
            "  - No free gap fits: Pay parking ticket (30m), Renew library card (15m), Draft the budget (2h)"])

    def test_a_new_plan_with_no_blocks_removes_the_old_file(self):
        self.keep()
        self.assertTrue(self.path(BLOCKS).exists())
        self.calendar(calendar(event("conf", "Conference", "20260928T080000", "20260928T220000")))
        Planner(self.agent).redo(self.agent.vault(), NOW)
        self.assertFalse(self.path(BLOCKS).exists())

    def test_the_run_says_once_that_it_made_the_plan(self):
        self.assertIn("Planned 3 blocks for today", self.keep())
        self.assertNotIn("Planned 3 blocks for today", self.keep(NOW + timedelta(minutes=5)))

    def test_the_runner_gives_no_time_and_the_plan_still_comes(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW
        with mock.patch("gtd_agent.planner.datetime", Clock):
            Planner(self.agent).run(self.agent.vault(), None)  # the runner's Housekeeper.run() has no time to pass on
        self.assertIsNotNone(Planner(self.agent).saved(TODAY))

    def test_old_plans_are_forgotten(self):
        self.agent.ledger.kv_set("plan|2026-09-01", "{}")
        self.agent.ledger.kv_set("plan|2026-09-20", "{}")
        self.keep()
        self.assertEqual(sorted(self.agent.ledger.kv_items("plan|")), ["plan|2026-09-20", "plan|2026-09-28"])


class MarkerTests(PlanCase):
    PARKING = "Pay parking ticket · 30m guessed · 3 days overdue · [[SINGLE_ACTIONS]]"

    def test_a_ticked_task_shows_as_done(self):
        self.keep()
        self.write(SINGLE, self.read(SINGLE).replace("- [ ] Pay parking ticket #computer 📅 2026-09-25",
                                                     "- [x] Pay parking ticket #computer 📅 2026-09-25 ✅ 2026-09-28"))
        self.keep(NOW + timedelta(minutes=5))
        self.assertEqual(self.plan()[1], f"  - ✓ 10:00–10:30 {self.PARKING}")

    def test_a_task_that_left_the_lists_shows_as_done(self):
        self.keep()
        self.write(SINGLE, self.read(SINGLE).replace("- [ ] Pay parking ticket #computer 📅 2026-09-25\n", ""))  # archived
        self.keep(NOW + timedelta(minutes=5))
        self.assertEqual(self.plan()[1], f"  - ✓ 10:00–10:30 {self.PARKING}")

    def test_a_repeating_task_done_last_week_is_not_done_today(self):
        self.write(SINGLE, self.read(SINGLE).replace(
            "- [ ] Pay parking ticket", "- [x] Pay parking ticket #computer 📅 2026-09-18 ✅ 2026-09-18\n- [ ] Pay parking ticket"))
        self.keep()
        self.assertEqual(self.plan()[1], f"  - 10:00–10:30 {self.PARKING}")
        self.keep(NOW + timedelta(minutes=5))
        self.assertEqual(self.plan()[1], f"  - 10:00–10:30 {self.PARKING}")

    def test_a_block_that_has_passed_with_its_task_open_is_late(self):
        self.keep()
        self.keep(datetime(2026, 9, 28, 11, 0))
        self.assertEqual(self.plan()[1:4], [
            f"  - late 10:00–10:30 {self.PARKING}",
            "  - late 10:30–10:45 Renew library card · 15m · due today · [[SINGLE_ACTIONS]]",
            "  - 13:00–15:00 Draft the budget · 2h · due Wed Sep 30 · [[DEMO_PROJECT]]"])

    def test_a_block_that_a_new_event_now_overlaps_gets_a_warning(self):
        self.keep()
        self.calendar(DAY.replace("END:VCALENDAR", event("call", "Call", "20260928T101500", "20260928T103000")
                                  + "END:VCALENDAR"))
        self.keep(NOW + timedelta(minutes=5))
        self.assertEqual(self.plan()[1], f"  - 10:00–10:30 {self.PARKING} · Conflict: overlaps Call (10:15–10:30)")
        self.assertEqual(self.plan()[2], "  - 10:30–10:45 Renew library card · 15m · due today · [[SINGLE_ACTIONS]]")


LATIN_TEXT = """---
key: LATIN
type: project
status: someday
area: "[[AREA_CLUB]]"
---
# LATIN

## Steps
- [ ] Buy the textbook #errands 📅 2026-09-25
"""


class LeftOutTests(PlanCase):
    plan_config = "[plan]\ntasks = 6\n"  # room for every task that may count; each one below would outrank them

    def extra_files(self) -> dict[str, str]:
        files = super().extra_files()
        files[SINGLE] = SINGLE_TEXT.replace("## @anywhere", """- [ ] Sort photos #computer ⏳ 2026-10-01
- [ ] File taxes #computer #next ⛔ form1 📅 2026-09-25
- [ ] Mail the form #computer 🆔 form1
- [ ] Book flights #computer 🛫 2026-10-02 📅 2026-09-25
- [x] Old thing #computer 📅 2026-09-20

## @anywhere""")
        files[LATIN] = LATIN_TEXT
        return files

    def test_waiting_someday_not_started_scheduled_for_later_blocked_and_done_tasks_are_never_planned(self):
        self.keep()
        self.assertEqual(self.plan(), [
            "- Plan for today:",
            "  - 10:00–10:30 Pay parking ticket · 30m guessed · 3 days overdue · [[SINGLE_ACTIONS]]",
            "  - 10:30–10:45 Renew library card · 15m · due today · [[SINGLE_ACTIONS]]",
            "  - 10:45–11:15 Email the advisor · 30m · [[DEMO_PROJECT]]",
            "  - 11:15–11:30 Call the dentist · 15m · [[AREA_ADMIN]]",
            "  - 11:30–12:00 Mail the form · 30m guessed · [[SINGLE_ACTIONS]]",
            "  - 13:00–15:00 Draft the budget · 2h · due Wed Sep 30 · [[DEMO_PROJECT]]",
            "  - Calendar file: `_agent/time-blocks.ics`. Import it into Proton Calendar."])


class ScheduledTests(PlanCase):
    def extra_files(self) -> dict[str, str]:
        files = super().extra_files()
        files[LAB] = LAB_TEXT.replace("- [ ] Book the room #calls", "- [ ] Book the room #calls ⏳ 2026-09-28")
        return files

    def test_a_task_scheduled_for_today_ranks_after_those_due_and_before_those_due_later(self):
        self.keep()
        self.assertEqual(self.plan()[3], "  - 10:45–11:15 Book the room · 30m guessed · scheduled today · [[DEMO_PROJECT]]")
        self.assertNotIn("Draft the budget", "\n".join(self.plan()))


class LongTaskTests(PlanCase):
    def extra_files(self) -> dict[str, str]:
        files = super().extra_files()
        files[LAB] = LAB_TEXT.replace("Draft the budget #computer #next #2h", "Draft the budget #computer #next #4h")
        return files

    def test_a_task_longer_than_the_longest_block_is_planned_in_parts(self):
        self.keep()
        self.assertEqual(self.plan()[3], "  - 13:00–15:00 Draft the budget · 2h of 4h · due Wed Sep 30 · [[DEMO_PROJECT]]")


class NoRoomTests(PlanCase):
    # Free: 10:45-11:00 and 21:00-22:00
    ics = calendar(event("a", "Busy", "20260928T090000", "20260928T104500"),
                   event("b", "Later", "20260928T110000", "20260928T210000"))

    def test_a_task_that_fits_nowhere_is_named_and_the_next_one_still_goes_in(self):
        self.keep()
        self.assertEqual(self.plan(), [
            "- Plan for today:",
            "  - 10:45–11:00 Renew library card · 15m · due today · [[SINGLE_ACTIONS]]",
            "  - 21:00–21:30 Pay parking ticket · 30m guessed · 3 days overdue · [[SINGLE_ACTIONS]]",
            "  - 21:30–22:00 Email the advisor · 30m · [[DEMO_PROJECT]]",
            "  - No free gap fits: Draft the budget (2h)",
            "  - Calendar file: `_agent/time-blocks.ics`. Import it into Proton Calendar."])


class NothingToPlanTests(PlanCase):
    def extra_files(self) -> dict[str, str]:
        empty = "---\nkey: {0}\ntype: project\nstatus: active\narea: \"[[AREA_CLUB]]\"\n---\n# {0}\n\n## Steps\n"
        return {SINGLE: "# SINGLE_ACTIONS\n\n## @computer\n\n## Waiting For\n",
                LAB: empty.format("DEMO_PROJECT"), TAXES: empty.format("TAXES")}

    def test_an_empty_plan_is_not_kept_so_a_task_that_arrives_is_planned(self):
        self.keep()
        self.assertEqual(self.plan(), ["- Plan for today: nothing to place. Add a `#next` step to an active project."])
        self.assertIn("- Load: 0h of work, ", self.read("_agent/TODAY.md"))
        self.assertFalse(self.path(BLOCKS).exists())
        self.write(SINGLE, self.read(SINGLE).replace("## @computer\n", "## @computer\n- [ ] Pay rent #computer #30m\n"))
        self.keep(NOW + timedelta(minutes=5))
        self.assertEqual(self.plan()[:2], ["- Plan for today:", "  - 10:00–10:30 Pay rent · 30m · [[SINGLE_ACTIONS]]"])


class PrivateTests(PlanCase):
    def extra_files(self) -> dict[str, str]:
        files = super().extra_files()
        files[SINGLE] = SINGLE_TEXT.replace("Call the dentist [[AREA_ADMIN]] #calls #15m",
                                            "Call the dentist [[AREA_ADMIN]] #calls #15m 📅 2026-09-20")
        return files

    def test_a_private_task_is_planned_and_marked_private_for_anything_that_leaves_the_vault(self):
        self.keep()
        self.assertEqual(self.plan()[1], "  - 10:00–10:15 Call the dentist · 15m · 8 days overdue · [[AREA_ADMIN]]")
        items = Planner(self.agent).saved(TODAY).items
        self.assertEqual({i.title: i.private for i in items},
                         {"Call the dentist": True, "Pay parking ticket": False, "Renew library card": False})


class NoCalendarTests(VaultCase):
    def extra_files(self) -> dict[str, str]:
        return {SINGLE: SINGLE_TEXT, LAB: LAB_TEXT, TAXES: TAXES_TEXT}

    def test_without_a_calendar_the_top_tasks_come_without_times_or_a_file(self):
        Housekeeper(self.agent).run(now=NOW)
        self.assertEqual(plan_of(self.read("_agent/TODAY.md")), [
            "- Top 3 for today (no times: calendar off):",
            "  - Pay parking ticket · 30m guessed · 3 days overdue · [[SINGLE_ACTIONS]]",
            "  - Renew library card · 15m · due today · [[SINGLE_ACTIONS]]",
            "  - Draft the budget · 2h · due Wed Sep 30 · [[DEMO_PROJECT]]"])
        self.assertFalse(self.path(BLOCKS).exists())


class CalendarUnreadableTests(PlanCase):
    def setUp(self) -> None:
        super().setUp()
        (self.settings.state_dir / "calendar" / "cache.ics").unlink()

    def test_a_calendar_that_cannot_be_read_gives_the_top_tasks_and_keeps_nothing(self):
        with mock.patch("gtd_agent.housekeeping.fetch_ics", return_value=(None, "Calendar unavailable (URLError)")):
            self.keep()
        self.assertEqual(self.plan()[0], "- Top 3 for today (no times: calendar unavailable):")
        self.assertIn("- Load: 2h 45m of work this week. Free time unknown: calendar unavailable. ", self.read("_agent/TODAY.md"))
        self.assertFalse(self.path(BLOCKS).exists())
        self.calendar(DAY)  # back again
        self.keep(NOW + timedelta(minutes=5))
        self.assertEqual(self.plan(), STANDARD)

    def test_the_calendar_is_asked_for_once_a_run(self):
        with mock.patch("gtd_agent.housekeeping.fetch_ics", return_value=(DAY, None)) as fetch:
            self.keep()
        self.assertEqual(fetch.call_count, 1)


class LoadCase(PlanCase):
    """Free time counts only 09:00-11:00 on Monday and Tuesday, so the sums stay small enough to read."""
    day_end = "11:00"

    def extra_config(self) -> str:
        return ('[calendar]\nenabled = true\nsource = "ics"\nday_start = "09:00"\n' + f'day_end = "{self.day_end}"\n'
                '[plan]\ndays = ["mon", "tue"]\n')

    def load(self, now: datetime = NOW) -> str:
        self.keep(now)
        return next(line for line in self.read("_agent/TODAY.md").splitlines() if line.startswith("- Load:"))


class LoadTests(LoadCase):
    # Work this week: Pay parking ticket 30m (no tag), Renew library card 15m, Draft the budget 2h. Free: Monday
    # 10:00-11:00 (the stand-up takes the first hour) and all of Tuesday 09:00-11:00.
    def test_the_work_due_this_week_against_the_free_time_left(self):
        self.assertEqual(self.load(), "- Load: 2h 45m of work, 3h free this week. Counted 1 task at 30m: no time tag.")

    def test_a_week_too_full_says_by_how_much(self):
        self.day_end = "10:15"  # free: Monday 10:00-10:15 and Tuesday 09:00-10:15
        self.setUp()
        self.assertEqual(self.load(), "- Load: 2h 45m of work, 1h 30m free this week. Over by 1h 15m: "
                                      "move or drop something. Counted 1 task at 30m: no time tag.")

    def test_free_time_already_gone_today_does_not_count(self):
        # from 10:30 Monday has 30 minutes left; Tuesday has two hours
        self.assertEqual(self.load(datetime(2026, 9, 28, 10, 30)),
                         "- Load: 2h 45m of work, 2h 30m free this week. Over by 15m: move or drop something. "
                         "Counted 1 task at 30m: no time tag.")

    def test_at_the_weekend_it_looks_at_the_week_ahead(self):
        self.agent._today = date(2026, 10, 3)  # Saturday
        self.assertEqual(self.load(datetime(2026, 10, 3, 9, 0)),
                         "- Load: 2h 45m of work, 4h free next week. Counted 1 task at 30m: no time tag.")


class LoadEqualTests(LoadCase):
    day_end = "10:45"  # free: Monday 10:00-10:45 and Tuesday 09:00-10:45, two and a half hours

    def extra_files(self) -> dict[str, str]:
        files = super().extra_files()
        files[LAB] = LAB_TEXT.replace("#2h", "#105m")  # 30 + 15 + 105 minutes of work
        return files

    def test_a_week_that_just_fits_is_not_over(self):
        self.assertEqual(self.load(), "- Load: 2h 30m of work, 2h 30m free this week. Counted 1 task at 30m: no time tag.")


class LoadCountsTests(LoadCase):
    def extra_files(self) -> dict[str, str]:
        files = super().extra_files()
        files[SINGLE] = """# SINGLE_ACTIONS

## @computer
- [ ] Pay parking ticket #computer 📅 2026-09-25
- [ ] Scheduled thing #computer #15m ⏳ 2026-10-04
- [ ] Later thing #computer #15m 📅 2026-10-10
- [ ] Sunday thing #computer 📅 2026-10-04
- [ ] File taxes #computer #15m ⛔ form1 📅 2026-09-30
- [ ] Mail the form #computer #15m 🆔 form1 📅 2026-09-30
- [x] Old thing #computer #15m 📅 2026-09-20

## Waiting For
- [ ] #waiting Riley: signed form ➕ 2026-09-15 📅 2026-09-25
"""
        files[LATIN] = LATIN_TEXT.replace("#errands", "#errands #15m")
        return files

    def test_only_open_unblocked_tasks_due_or_scheduled_by_the_end_of_the_week_count(self):
        # parking 30 + scheduled thing 15 + mail the form 15 + the budget 120 + Sunday's thing 30; not the later,
        # blocked, done, waiting, someday or undated tasks
        self.assertEqual(self.load(), "- Load: 3h 30m of work, 3h free this week. Over by 30m: move or drop something. "
                                      "Counted 2 tasks at 30m: no time tag.")


class LoadWithoutCalendarTests(VaultCase):
    def extra_files(self) -> dict[str, str]:
        return {SINGLE: SINGLE_TEXT, LAB: LAB_TEXT, TAXES: TAXES_TEXT}

    def test_free_time_is_unknown_without_a_calendar(self):
        Housekeeper(self.agent).run(now=NOW)
        line = next(l for l in self.read("_agent/TODAY.md").splitlines() if l.startswith("- Load:"))
        self.assertEqual(line, "- Load: 2h 45m of work this week. Free time unknown: calendar off. "
                               "Counted 1 task at 30m: no time tag.")


class LoadReviewTests(LoadCase):
    def test_the_weekly_review_carries_the_load(self):
        Housekeeper(self.agent).weekly(self.agent.vault(), datetime(2026, 10, 4, 9, 0), force=True)
        note = self.read("01_GTD/REVIEWS/2026-W40.md")
        self.assertIn("### Load\n- 2h 45m of work, 3h free this week. Counted 1 task at 30m: no time tag.\n", note)


class CommandTests(PlanCase):
    """run.ps1 plan shows today's plan; run.ps1 plan --redo makes it again from now."""

    def command(self, *words: str) -> tuple[int, list[str]]:
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("gtd_agent.cli.datetime", Clock), \
                mock.patch.object(Agent, "today", lambda self: TODAY):
            code = main(["--config", str(self.settings.config_path), "plan", *words])
        return code, out.getvalue().splitlines()

    def test_plan_shows_the_plan_and_the_load_without_changing_anything(self):
        code, lines = self.command()
        self.assertEqual(code, 0)
        self.assertEqual(lines[:4], STANDARD[:4])
        self.assertTrue(lines[-1].startswith("- Load: 2h 45m of work, "))
        self.assertFalse(self.path(BLOCKS).exists())

    def test_redo_keeps_the_plan_and_writes_the_file(self):
        code, lines = self.command("--redo")
        self.assertEqual(code, 0)
        self.assertEqual(lines[:5], STANDARD)
        self.assertEqual(len(self.blocks()), 3)
        self.assertEqual(Planner(self.agent).saved(TODAY).items[0].title, "Pay parking ticket")


if __name__ == "__main__":
    unittest.main()
