import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import helpers
from gtd_agent.calendar_ics import event_span, free_gaps, parse_events
from gtd_agent.core import (Settings, Snapshot, _mini_toml, atomic_replace, digest, parse_captures, write_owned)
from gtd_agent.credentials import get_secret, set_secret

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:class
SUMMARY:ACCT 423 lecture
DTSTART;TZID=America/New_York:20260915T100000
DTEND;TZID=America/New_York:20260915T111500
RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261210
EXDATE;TZID=America/New_York:20260929T100000
END:VEVENT
BEGIN:VEVENT
UID:class
RECURRENCE-ID;TZID=America/New_York:20261001T100000
SUMMARY:ACCT 423 moved
DTSTART;TZID=America/New_York:20261001T140000
DTEND;TZID=America/New_York:20261001T151500
END:VEVENT
BEGIN:VEVENT
UID:gmat
SUMMARY:GMAT
DTSTART;VALUE=DATE:20261011
DTEND;VALUE=DATE:20261012
END:VEVENT
BEGIN:VEVENT
UID:board
SUMMARY:DEMO_CLUB board
  meeting
DTSTART;TZID=America/New_York:20260907T180000
DURATION:PT1H
RRULE:FREQ=MONTHLY;BYDAY=1MO;COUNT=4
END:VEVENT
BEGIN:VEVENT
UID:gone
SUMMARY:Cancelled thing
STATUS:CANCELLED
DTSTART;TZID=America/New_York:20261001T090000
DTEND;TZID=America/New_York:20261001T100000
END:VEVENT
END:VCALENDAR
"""


class CalendarTests(unittest.TestCase):
    def events(self, start, days):
        return parse_events(ICS, start, start + timedelta(days=days))

    def test_weekly_rule_exdate_and_override(self):
        events = self.events(datetime(2026, 9, 28), 7)
        summaries = [(e.start, e.summary) for e in events]
        self.assertNotIn((datetime(2026, 9, 29, 10, 0), "ACCT 423 lecture"), summaries)  # EXDATE
        self.assertIn((datetime(2026, 10, 1, 14, 0), "ACCT 423 moved"), summaries)       # RECURRENCE-ID
        self.assertNotIn((datetime(2026, 10, 1, 10, 0), "ACCT 423 lecture"), summaries)
        self.assertNotIn("Cancelled thing", [s for _, s in summaries])

    def test_monthly_nth_weekday_count_and_folding(self):
        events = self.events(datetime(2026, 9, 1), 150)
        board = [e.start for e in events if e.summary == "DEMO_CLUB board meeting"]
        self.assertEqual(board, [datetime(2026, 9, 7, 18), datetime(2026, 10, 5, 18), datetime(2026, 11, 2, 18),
                                 datetime(2026, 12, 7, 18)])

    def test_all_day_and_utc(self):
        events = self.events(datetime(2026, 10, 11), 1)
        self.assertEqual([(e.summary, e.all_day, e.busy) for e in events], [("GMAT", True, False)])
        utc = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Call\nDTSTART:20261002T130000Z\nDTEND:20261002T140000Z\n"
               "END:VEVENT\nEND:VCALENDAR\n")
        event = parse_events(utc, datetime(2026, 10, 1), datetime(2026, 10, 3))[0]
        expected = datetime(2026, 10, 2, 13, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        self.assertEqual(event.start, expected)

    def test_one_event_listed_twice_in_the_feed_shows_once(self):
        def block(sequence: int, start: str) -> str:
            return ("BEGIN:VEVENT\nUID:reading\nSUMMARY:Morning reading\n"
                    + (f"SEQUENCE:{sequence}\n" if sequence else "")
                    + f"DTSTART;TZID=America/New_York:{start}\nDURATION:PT10M\nRRULE:FREQ=DAILY\nEND:VEVENT\n")
        feed = "BEGIN:VCALENDAR\n" + block(0, "20260607T080000") * 3 + "END:VCALENDAR\n"  # Proton's link repeats it
        events = parse_events(feed, datetime(2026, 10, 1), datetime(2026, 10, 2))
        self.assertEqual([(e.summary, e.start) for e in events], [("Morning reading", datetime(2026, 10, 1, 8, 0))])
        moved = "BEGIN:VCALENDAR\n" + block(2, "20260607T090000") + block(1, "20260607T080000") + "END:VCALENDAR\n"
        events = parse_events(moved, datetime(2026, 10, 1), datetime(2026, 10, 2))
        self.assertEqual([e.start for e in events], [datetime(2026, 10, 1, 9, 0)])  # the newer copy wins

    def test_free_gaps(self):
        events = self.events(datetime(2026, 10, 1), 1)
        gaps = free_gaps(events, date(2026, 10, 1), (8, 0), (18, 0), None, 30)
        self.assertEqual(gaps, [(datetime(2026, 10, 1, 8), datetime(2026, 10, 1, 14)),
                                (datetime(2026, 10, 1, 15, 15), datetime(2026, 10, 1, 18))])
        later = free_gaps(events, date(2026, 10, 1), (8, 0), (18, 0), datetime(2026, 10, 1, 16, 50), 30)
        self.assertEqual(later, [(datetime(2026, 10, 1, 16, 50), datetime(2026, 10, 1, 18))])


def one_event(extra: str = "", head: str = "") -> str:
    return ("BEGIN:VCALENDAR\r\n" + head + "BEGIN:VEVENT\r\nUID:x\r\nSUMMARY:Checkup\r\n"
            "DTSTART:20261001T140000Z\r\nDTEND:20261001T150000Z\r\n" + extra + "END:VEVENT\r\nEND:VCALENDAR\r\n")


class PrivateEventTests(unittest.TestCase):
    """An event the calendar marks private is never named outside the calendar (a phone ping, for one)."""

    def events(self, text: str):
        return parse_events(text, datetime(2026, 10, 1), datetime(2026, 10, 3))

    def test_an_event_is_not_private_unless_the_calendar_says_so(self):
        self.assertFalse(self.events(one_event())[0].private)
        self.assertFalse(self.events(one_event("CLASS:PUBLIC\r\n"))[0].private)

    def test_private_and_confidential_events_are_private_in_any_case(self):
        for value in ("PRIVATE", "CONFIDENTIAL", "private"):
            with self.subTest(value):
                self.assertTrue(self.events(one_event(f"CLASS:{value}\r\n"))[0].private)

    def test_every_repeat_of_a_private_event_is_private(self):
        found = self.events(one_event("CLASS:PRIVATE\r\nRRULE:FREQ=DAILY;COUNT=2\r\n"))
        self.assertEqual([e.private for e in found], [True, True])

    def test_a_moved_repeat_keeps_its_own_class(self):
        moved = one_event("CLASS:PRIVATE\r\nRRULE:FREQ=DAILY;COUNT=2\r\n").replace(
            "END:VCALENDAR", "BEGIN:VEVENT\r\nUID:x\r\nRECURRENCE-ID:20261002T140000Z\r\nSUMMARY:Checkup\r\n"
            "DTSTART:20261002T160000Z\r\nDTEND:20261002T170000Z\r\nEND:VEVENT\r\nEND:VCALENDAR")
        self.assertEqual([e.private for e in self.events(moved)], [True, False])


class EventSpanTests(unittest.TestCase):
    """How TODAY and the phone brief word the time an event takes on one day."""
    DAY = datetime(2026, 10, 1)

    def span(self, start: datetime, end: datetime) -> str:
        return event_span(helpers_event(start, end), self.DAY)

    def test_an_event_inside_the_day_has_both_times(self):
        self.assertEqual(self.span(datetime(2026, 10, 1, 9), datetime(2026, 10, 1, 10, 30)), "09:00–10:30")

    def test_an_event_that_began_before_the_day_is_until(self):
        self.assertEqual(self.span(datetime(2026, 9, 30, 22), datetime(2026, 10, 1, 7)), "until 07:00")

    def test_an_event_that_runs_past_the_day_names_the_weekday_it_ends(self):
        self.assertEqual(self.span(datetime(2026, 10, 1, 23), datetime(2026, 10, 2, 1)), "23:00 until Fri 01:00")

    def test_an_event_that_covers_the_whole_day_is_all_day(self):
        self.assertEqual(self.span(datetime(2026, 9, 30, 12), datetime(2026, 10, 2, 12)), "all day")

    def test_an_event_that_began_before_the_day_and_lasts_to_its_end_is_all_day(self):
        self.assertEqual(self.span(datetime(2026, 9, 30, 12), datetime(2026, 10, 2, 0)), "all day")

    def test_an_event_that_starts_at_midnight_and_runs_on_keeps_its_times(self):
        self.assertEqual(self.span(datetime(2026, 10, 1, 0), datetime(2026, 10, 2, 12)), "00:00 until Fri 12:00")

    def test_an_event_that_starts_at_midnight_did_not_begin_earlier(self):
        self.assertEqual(self.span(datetime(2026, 10, 1, 0), datetime(2026, 10, 1, 7)), "00:00–07:00")

    def test_an_event_that_ends_as_the_day_does_is_inside_it(self):
        self.assertEqual(self.span(datetime(2026, 10, 1, 23), datetime(2026, 10, 2, 0)), "23:00–00:00")


def helpers_event(start: datetime, end: datetime):
    from gtd_agent.calendar_ics import Event
    return Event("Thing", start, end, False, True)


class CoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)

    def test_mini_toml_matches_the_config_format(self):
        data = _mini_toml('[vault]\nroot = "C:/Users/me/VAULT" # comment\n[agent]\nmode = "approval"\n'
                          'poll_seconds = 10\nauto = true\n[telegram]\nchat_id = 12345\n')
        self.assertEqual(data["vault"]["root"], "C:/Users/me/VAULT")
        self.assertEqual(data["agent"], {"mode": "approval", "poll_seconds": 10, "auto": True})
        self.assertEqual(data["telegram"]["chat_id"], 12345)

    def test_settings_reject_state_inside_vault(self):
        (self.base / "v").mkdir()
        config = self.base / "c.toml"
        config.write_text(f'[vault]\nroot = "{(self.base / "v").as_posix()}"\n[agent]\n'
                          f'state_dir = "{(self.base / "v" / "state").as_posix()}"\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside"):
            Settings.load(config)

    def test_the_inbox_is_quiet_after_30_seconds_unless_config_says_otherwise(self):
        (self.base / "v").mkdir()
        config = self.base / "c.toml"
        head = (f'[vault]\nroot = "{(self.base / "v").as_posix()}"\n[agent]\n'
                f'state_dir = "{(self.base / "s").as_posix()}"\n')
        config.write_text(head, encoding="utf-8")
        self.assertEqual(Settings.load(config).inbox_quiet_seconds, 30)
        config.write_text(head + "inbox_quiet_seconds = 0\n", encoding="utf-8")  # 0: read the Inbox at every scan
        self.assertEqual(Settings.load(config).inbox_quiet_seconds, 0)
        config.write_text(head + "inbox_quiet_seconds = -5\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inbox_quiet_seconds cannot be negative"):
            Settings.load(config)

    def test_version_1_config_is_refused(self):
        (self.base / "v").mkdir()
        config = self.base / "c.toml"
        config.write_text(f'[vault]\nroot = "{(self.base / "v").as_posix()}"\n'
                          'next_actions = "GTD_DAILY/NEXT_ACTIONS.md"\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "version 1"):
            Settings.load(config)

    def test_captures_skip_comments_and_strip_done_dates(self):
        text = "%% note %%\n%%\nmulti\nline\n%%\n# Heading\n- [x] Call Sam ✅ 2026-09-28\nplain line\n"
        captures = parse_captures(Snapshot(Path("x"), text, "h", "\n"), 6000)
        self.assertEqual([(c.text, c.checked) for c in captures], [("Call Sam", True), ("plain line", False)])

    def test_atomic_replace_guards_and_retries(self):
        note = self.base / "n.md"
        note.write_text("original", encoding="utf-8")
        state = self.base / "state"
        replace = os.replace
        calls = 0

        def locked_then_free(source, target):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise PermissionError("sharing lock")
            replace(source, target)

        with patch("gtd_agent.core.os.replace", side_effect=locked_then_free), patch("gtd_agent.core.time.sleep"):
            atomic_replace(note, "updated", state, digest("original"))
        self.assertEqual(note.read_text(), "updated")
        self.assertEqual(next((state / "backups").iterdir()).read_text(), "original")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            atomic_replace(note, "stale", state, digest("original"))
        self.assertTrue(write_owned(self.base / "own.md", "a\n", state))
        self.assertFalse(write_owned(self.base / "own.md", "a\n", state))

    def test_crlf_files_keep_their_line_endings(self):
        note = self.base / "w.md"
        note.write_bytes(b"one\r\ntwo\r\n")
        from gtd_agent.core import read_snapshot
        snap = read_snapshot(note)
        self.assertEqual(snap.text, "one\ntwo\n")
        atomic_replace(note, snap.text + "three\n", self.base / "s", snap.sha256, snap.newline)
        self.assertEqual(note.read_bytes(), b"one\r\ntwo\r\nthree\r\n")


class SpanAndRepeatTests(unittest.TestCase):
    def test_review_spans(self):
        from gtd_agent.dates import resolve_date_evidence
        ref = date(2026, 9, 28)
        cases = {"in a year": date(2027, 9, 28), "6 months from now": date(2027, 3, 28), "next year": date(2027, 1, 1),
                 "in March": date(2027, 3, 1), "in 2 weeks": date(2026, 10, 12), "end of the year": date(2026, 12, 31)}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(resolve_date_evidence(text, ref), expected)
        with self.assertRaises(ValueError):
            resolve_date_evidence("after my certification exams", ref)

    def test_repeat_rules(self):
        from gtd_agent.clarification import clean_recurrence, first_due
        self.assertEqual(clean_recurrence("every week on monday"), "every week on Monday")
        self.assertEqual(clean_recurrence("Every January on the 15th"), "every January on the 15th")
        for bad in ("sometimes", "every blue moon", "every week; rm -rf"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clean_recurrence(bad)
        ref = date(2026, 9, 29)  # a Tuesday
        self.assertEqual(first_due("every week on Monday", ref), date(2026, 10, 5))
        self.assertEqual(first_due("every month on the 15th", ref), date(2026, 10, 15))
        self.assertEqual(first_due("every January on the 15th", ref), date(2027, 1, 15))
        self.assertEqual(first_due("every day", ref), ref)


class LedgerImportTests(unittest.TestCase):
    def test_v1_lessons_import_once(self):
        import json
        import sqlite3
        from gtd_agent.ledger import Ledger
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            old = sqlite3.connect(state / "agent.sqlite3")
            old.execute("CREATE TABLE learned_cases (approval_id TEXT PRIMARY KEY, example_json TEXT NOT NULL, "
                        "active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)")
            old.executemany("INSERT INTO learned_cases VALUES (?,?,?,?)", [
                ("a1", json.dumps({"capture": "x", "route": "existing_project", "lesson": "l"}), 1, "2026-09-26"),
                ("a2", json.dumps({"capture": "y", "route": "next_action"}), 0, "2026-09-27")])
            old.commit()
            old.close()
            ledger = Ledger(state)
            self.assertEqual(ledger.import_v1_once(state), "Imported 1 learned examples from version 1")
            self.assertIsNone(ledger.import_v1_once(state))
            rows = ledger.list_learning()
            self.assertEqual([r["approval_id"] for r in rows], ["v1-a1"])
            self.assertEqual(json.loads(rows[0]["example_json"])["route"], "project_note")
            ledger.connection.close()


class CredentialTests(unittest.TestCase):
    def test_env_then_file(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            with patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(get_secret("calendar", state))
                if os.name != "nt":
                    set_secret("calendar", "https://example.test/cal.ics", state)
                    self.assertEqual(get_secret("calendar", state), "https://example.test/cal.ics")
                    self.assertEqual(oct((state / "credentials" / "secrets.env").stat().st_mode & 0o777), "0o600")
            with patch.dict(os.environ, {"GTD_CALENDAR_ICS_URL": "https://env.test"}):
                self.assertEqual(get_secret("calendar", state), "https://env.test")


if __name__ == "__main__":
    unittest.main()
