"""The Radicale calendar: read events from its storage folder, add approved events over CalDAV, undo them."""
import base64
import dataclasses
import hashlib
import json
import os
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from helpers import TODAY, VaultCase, draft
from gtd_agent.calendar_ics import rrule_from_tasks
from gtd_agent.housekeeping import Housekeeper
from gtd_agent.workflow import ApprovalError

EASTERN = """BEGIN:VTIMEZONE
TZID:America/New_York
BEGIN:STANDARD
DTSTART:20071104T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
TZNAME:EST
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:20070311T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
TZNAME:EDT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
END:DAYLIGHT
END:VTIMEZONE
"""


def phone_item(uid, summary, start, end):
    """An event the way DAVx5 stores it from the phone."""
    return ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:DAVx5/4.5.19-ose ical4j/4.3.0\n" + EASTERN +
            f"BEGIN:VEVENT\nUID:{uid}\nDTSTART;TZID=America/New_York:{start}\nDTEND;TZID=America/New_York:{end}\n"
            f"DTSTAMP:20260927T024405Z\nSTATUS:CONFIRMED\nSUMMARY:{summary}\nBEGIN:VALARM\nACTION:DISPLAY\n"
            f"DESCRIPTION:{summary}\nTRIGGER:-PT10M\nEND:VALARM\nEND:VEVENT\nEND:VCALENDAR\n")


def make_collection(root, user, folder, displayname, components, items):
    path = root / "collection-root" / user / folder
    (path / ".Radicale.cache" / "item").mkdir(parents=True)
    (path / ".Radicale.props").write_text(json.dumps({"D:displayname": displayname, "tag": "VCALENDAR",
                                                      "C:supported-calendar-component-set": components}))
    for name, text in items.items():
        (path / name).write_text(text, encoding="utf-8")
    # Radicale's cache holds pickled copies named like items; reading them would break parsing.
    (path / ".Radicale.cache" / "item" / "cached.ics").write_bytes(b"\x80\x04\x95 not an ics file")
    return path


class FakeRadicale(ThreadingHTTPServer):
    """A CalDAV server with Radicale's answers for PUT, GET and DELETE on items. Items live in memory."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeRadicaleHandler)
        self.items: dict[str, tuple[str, str]] = {}  # path -> (ics, etag)
        self.requests: list[tuple[str, str]] = []
        self.password = "radicale-pass"

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class FakeRadicaleHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _authorized(self):
        expected = "Basic " + base64.b64encode(f"taylor:{self.server.password}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def _done(self, status, etag=None, body=b""):
        self.send_response(status)
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        self.server.requests.append(("PUT", self.path))
        if not self._authorized():
            return
        body = self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")
        if self.headers.get("If-None-Match") == "*" and self.path in self.server.items:
            return self._done(412)
        if "BEGIN:VEVENT" not in body:
            return self._done(400)
        etag = '"' + hashlib.sha256(body.encode()).hexdigest()[:16] + '"'
        self.server.items[self.path] = (body, etag)
        self._done(201, etag)

    def do_GET(self):
        self.server.requests.append(("GET", self.path))
        if not self._authorized():
            return
        if self.path not in self.server.items:
            return self._done(404)
        body, etag = self.server.items[self.path]
        self._done(200, etag, body.encode("utf-8"))

    def do_DELETE(self):
        self.server.requests.append(("DELETE", self.path))
        if not self._authorized():
            return
        if self.path not in self.server.items:
            return self._done(404)
        if self.headers.get("If-Match") not in (None, self.server.items[self.path][1]):
            return self._done(412)
        del self.server.items[self.path]
        self._done(204)


class RadicaleCase(VaultCase):
    def extra_config(self):
        root = (self.base / "radicale").as_posix()
        return (f"[calendar]\nenabled = true\nsource = \"folder\"\nfolder = \"{root}\"\nuser = \"taylor\"\n"
                f"calendar = \"Personal\"\nurl = \"{self.server.url}\"\n")

    def setUp(self):
        self.server = FakeRadicale()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        secret = mock.patch.dict(os.environ, {"GTD_RADICALE_PASSWORD": "radicale-pass"})
        secret.start()
        self.addCleanup(secret.stop)
        super().setUp()
        root = self.base / "radicale"
        (root / "collection-root" / "taylor").mkdir(parents=True)
        (root / ".Radicale.lock").write_text("")
        self.personal = make_collection(root, "taylor", "845c00aa", "Personal", "VEVENT,VTODO", {
            "a1.ics": phone_item("a1", "Advisor meeting", "20260928T150000", "20260928T160000"),
            "a2.ics": phone_item("a2", "Next week", "20261005T090000", "20261005T100000")})
        make_collection(root, "taylor", "99aa", "Birthdays", "VEVENT", {
            "b1.ics": phone_item("b1", "Someone's birthday", "20260928T120000", "20260928T130000")})


class ReadTests(RadicaleCase):
    def test_today_lists_the_events_in_the_named_radicale_calendar(self):
        keeper = Housekeeper(self.agent)
        start = datetime(2026, 9, 28)
        events, warning = keeper.calendar_events(start, datetime(2026, 9, 29))
        self.assertIsNone(warning)
        self.assertEqual([(e.summary, e.start, e.end) for e in events],
                         [("Advisor meeting", datetime(2026, 9, 28, 15, 0), datetime(2026, 9, 28, 16, 0))])
        keeper.digest(self.agent.vault(), now=datetime(2026, 9, 28, 8, 0))
        self.assertIn("15:00–16:00 Advisor meeting", self.read("_agent/TODAY.md"))

    def test_a_missing_calendar_folder_is_a_plain_warning(self):
        import shutil
        shutil.rmtree(self.base / "radicale")
        events, warning = Housekeeper(self.agent).calendar_events(datetime(2026, 9, 28), datetime(2026, 9, 29))
        self.assertEqual(events, [])
        self.assertIn("Radicale calendar folder not found", warning)


class AddEventTests(RadicaleCase):
    def capture_event(self, line):
        self.add_inbox(line)
        return self.agent.scan().new_proposals[0]["proposal"]

    def test_a_timed_capture_becomes_an_approved_event_with_a_phone_alert(self):
        proposal = self.capture_event("Advisor meeting #event 2026-10-06 15:30-16:15")
        self.assertEqual(proposal["kind"], "calendar_event")
        self.assertEqual(proposal["summary"], "Event in Personal: Tue Oct 6 15:30–16:15, alert 10 min before")
        self.assertEqual(self.server.items, {})  # nothing written before approval
        self.tick()
        self.agent.sync_review_requests()
        [(path, (ics, _))] = self.server.items.items()
        self.assertTrue(path.startswith("/taylor/845c00aa/gtd-"), path)
        for line in ("BEGIN:VTIMEZONE", "TZID:America/New_York", "DTSTART;TZID=America/New_York:20261006T153000",
                     "DTEND;TZID=America/New_York:20261006T161500", "SUMMARY:Advisor meeting", "BEGIN:VALARM",
                     "TRIGGER:-PT10M"):
            self.assertIn(line + "\r\n", ics)
        self.assertNotIn("Advisor meeting", self.read("00_INBOX/INBOX.md"))
        self.assertIn("Approved · Calendar event: Advisor meeting", self.read("_agent/LOG.md"))

    def test_undo_removes_the_event_from_the_calendar(self):
        self.capture_event("Advisor meeting #event 2026-10-06 15:30-16:15")
        self.tick()
        self.agent.sync_review_requests()
        undo = self.agent.ledger.list_undo()[0]
        self.assertEqual(self.agent.undo(undo["id"]), "Reversed: Calendar event: Advisor meeting")
        self.assertEqual(self.server.items, {})

    def test_undo_stops_when_the_event_changed_on_the_phone(self):
        self.capture_event("Advisor meeting #event 2026-10-06 15:30-16:15")
        self.tick()
        self.agent.sync_review_requests()
        [(path, (ics, _))] = self.server.items.items()
        self.server.items[path] = (ics.replace("15:30", "16:00"), '"edited-on-phone"')
        with self.assertRaisesRegex(ApprovalError, "changed on your calendar"):
            self.agent.undo(self.agent.ledger.list_undo()[0]["id"])
        self.assertIn(path, self.server.items)

    def test_an_event_needs_a_day_and_a_time(self):
        proposal = self.capture_event("Dentist #event 2026-10-06")
        self.assertEqual(proposal["kind"], "manual")
        self.assertIn("needs a day and a time", proposal["reason"])

    def test_a_calendar_that_is_down_keeps_the_proposal_waiting(self):
        self.capture_event("Advisor meeting #event 2026-10-06 3:30pm")
        self.server.shutdown()
        self.server.server_close()
        self.tick()
        outcome = self.agent.sync_review_requests()
        self.assertTrue(any("Radicale is not answering" in line for line in outcome), outcome)
        self.assertEqual(len(self.pending("inbox")), 1)


class ModelEventTests(RadicaleCase):
    remote = True

    def test_a_meeting_in_plain_words_becomes_an_event_proposal(self):
        self.add_inbox("meet Nora on Tuesday at 3pm for an hour about the sponsorship")
        self.provider.decisions.append({"operation": ("new_capture", 0.9), "category": ("calendar", 0.9),
                                        "multiplicity": ("single", 0.9)})
        self.provider.interpretations.append(draft(route="calendar_event", title="Meet Nora about the sponsorship",
                                                   due_evidence="Tuesday", time_evidence="3pm",
                                                   end_evidence="for an hour"))
        proposal = self.agent.scan().new_proposals[0]["proposal"]
        self.assertEqual(proposal["kind"], "calendar_event")
        self.assertEqual(proposal["summary"], "Event in Personal: Tue Sep 29 15:00–16:00, alert 10 min before")

    def test_a_day_without_a_time_is_never_an_event(self):
        self.add_inbox("dentist on friday")
        self.provider.interpretations.append(draft(route="calendar_event", title="Dentist", due_evidence="friday"))
        proposal = self.agent.scan().new_proposals[0]["proposal"]
        self.assertEqual(proposal["kind"], "manual")
        self.assertIn("needs a day and a time", proposal["reason"])


class DoctorTests(RadicaleCase):
    def test_doctor_checks_the_calendar_folder_and_the_password(self):
        import io
        from contextlib import redirect_stdout
        from gtd_agent.cli import main
        out = io.StringIO()
        with redirect_stdout(out):
            main(["--config", str(self.settings.config_path), "doctor"])
        self.assertIn("OK: Radicale calendar 'Personal' (2 events)", out.getvalue())
        self.assertIn("OK: Radicale password saved", out.getvalue())
        with mock.patch.dict(os.environ, {"GTD_RADICALE_PASSWORD": ""}):
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--config", str(self.settings.config_path), "doctor"])
        self.assertIn("CHECK: Radicale password saved · missing: run set-secret radicale (needed to add events)",
                      out.getvalue())


class ExportTests(RadicaleCase):
    """Radicale retires: each calendar exports once as an .ics file to import into Proton Calendar."""

    def test_the_export_holds_every_radicale_event(self):
        import io
        from contextlib import redirect_stdout
        from gtd_agent.calendar_ics import parse_events
        from gtd_agent.cli import main
        repeating = phone_item("r1", "Gym", "20261001T070000", "20261001T080000").replace(
            "STATUS:CONFIRMED", "RRULE:FREQ=WEEKLY;BYDAY=TH\nSTATUS:CONFIRMED")
        (self.personal / "r1.ics").write_text(repeating, encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--config", str(self.settings.config_path), "calendar", "export", str(self.base / "out")])
        self.assertEqual(code, 0)
        personal = (self.base / "out" / "radicale-Personal.ics").read_bytes().decode("utf-8")
        birthdays = (self.base / "out" / "radicale-Birthdays.ics").read_bytes().decode("utf-8")
        self.assertIn("radicale-Personal.ics: 3 events", out.getvalue())
        for uid in ("a1", "a2", "r1"):
            self.assertIn(f"UID:{uid}\r\n", personal)
        self.assertIn("UID:b1\r\n", birthdays)
        self.assertEqual(personal.count("BEGIN:VTIMEZONE"), 1)
        self.assertTrue(personal.startswith("BEGIN:VCALENDAR\r\n") and personal.endswith("END:VCALENDAR\r\n"))
        events = parse_events(personal, datetime(2026, 9, 28), datetime(2026, 10, 9))
        self.assertEqual([e.summary for e in events], ["Advisor meeting", "Gym", "Next week", "Gym"])


class RepeatRuleTests(unittest.TestCase):
    def test_tasks_repeat_rules_become_calendar_rules(self):
        monday = datetime(2026, 9, 28).date()
        cases = {"every day": "FREQ=DAILY", "every 2 days": "FREQ=DAILY;INTERVAL=2",
                 "every weekday": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "every week": "FREQ=WEEKLY;BYDAY=MO",
                 "every week on Monday": "FREQ=WEEKLY;BYDAY=MO", "every Monday": "FREQ=WEEKLY;BYDAY=MO",
                 "every 2 weeks on Friday": "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR",
                 "every week on Monday, Wednesday": "FREQ=WEEKLY;BYDAY=MO,WE",
                 "every month on the 15th": "FREQ=MONTHLY;BYMONTHDAY=15", "every month": "FREQ=MONTHLY;BYMONTHDAY=28",
                 "every month on the last": "FREQ=MONTHLY;BYMONTHDAY=-1", "every year": "FREQ=YEARLY",
                 "every January on the 15th": "FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=15",
                 "every day when done": None, "every blue moon": None}
        for rule, expected in cases.items():
            with self.subTest(rule=rule):
                self.assertEqual(rrule_from_tasks(rule, monday), expected)


class TimedDutyTests(RadicaleCase):
    def extra_files(self):
        return {"03_AREAS/AREA_CLUB.md": (
            "---\nkey: AREA_CLUB\ntype: area\naliases: [Club]\nprivate: false\n---\n# AREA_CLUB\n\n## Recurring\n"
            "- [ ] Write board agenda (by 1:30pm) #computer #next 🔁 every week on Monday 🛫 2026-09-26 📅 2026-09-28\n"
            "- [ ] Water the plants #anywhere #next 🔁 every week on Sunday 🛫 2026-10-02 📅 2026-10-04\n"
            "- [ ] Tidy the desk (9am) #anywhere #next 🔁 every day when done 📅 2026-09-28\n")}

    def test_a_timed_duty_gets_a_repeating_event_with_an_alert_after_approval(self):
        Housekeeper(self.agent).run(now=datetime(2026, 9, 28, 8, 0))
        rows = self.pending("calendar")
        self.assertEqual(len(rows), 1)  # the untimed duty and the 'when done' one get none
        proposal = json.loads(rows[0]["proposal_json"])
        self.assertEqual(proposal["summary"], "Repeating event in Personal: every week on Monday at 13:30 from "
                                              "Mon Sep 28, alert 10 min before")
        self.tick("Approve", rows[0]["id"])
        self.agent.sync_review_requests()
        [(path, (ics, _))] = self.server.items.items()
        self.assertIn("RRULE:FREQ=WEEKLY;BYDAY=MO\r\n", ics)
        self.assertIn("DTSTART;TZID=America/New_York:20260928T133000\r\n", ics)
        self.assertIn("SUMMARY:Write board agenda (by 1:30pm)\r\n", ics)
        Housekeeper(self.agent).run(now=datetime(2026, 9, 28, 9, 0))
        self.assertEqual(self.pending("calendar"), [])  # asked once

    def test_after_the_move_to_proton_its_alert_proposals_go(self):
        Housekeeper(self.agent).run(now=datetime(2026, 9, 28, 8, 0))
        self.assertEqual(len(self.pending("calendar")), 1)
        self.agent.settings = dataclasses.replace(self.agent.settings, calendar_source="ics")
        Housekeeper(self.agent).run(now=datetime(2026, 9, 28, 9, 0))
        self.assertEqual(self.pending("calendar"), [])  # Proton is read only for the agent: its alerts end

    def test_a_rejected_alert_is_not_asked_again(self):
        Housekeeper(self.agent).run(now=datetime(2026, 9, 28, 8, 0))
        row = self.pending("calendar")[0]
        self.tick("Reject", row["id"])
        self.agent.sync_review_requests()
        Housekeeper(self.agent).run(now=datetime(2026, 9, 28, 9, 0))
        self.assertEqual(self.pending("calendar"), [])
        self.assertEqual(self.server.items, {})


if __name__ == "__main__":
    unittest.main()
