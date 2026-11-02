"""Phone pings (phase 3 item 5): what the agent sends you on Telegram besides answers to your own messages.

Three pings, each on its own rule: the morning brief, approvals that have waited, and a mail connection (Bridge)
that is down. Quiet hours hold all of them. Nothing here reaches Telegram: `FakeBot` takes every call.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import socket
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from datetime import date, timedelta

from helpers import VaultCase, make_settings, make_vault
from test_conflicts import calendar, event
from test_plan import DAY, LAB, LAB_TEXT, NOW, PlanCase, SINGLE, SINGLE_TEXT, TAXES, TAXES_TEXT
from test_telegram import FakeBot
from gtd_agent.housekeeping import Housekeeper
from gtd_agent.pings import Pings, quiet
from gtd_agent.telegram import TelegramError


class PingSettingsTests(unittest.TestCase):
    def settings(self, extra: str = ""):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        make_vault(base / "vault")
        return make_settings(base, extra=f"[telegram]\n{extra}" if extra else "")

    def test_by_default_every_ping_is_on_and_titles_are_named(self):
        s = self.settings()
        self.assertEqual(s.telegram_pings, ("brief", "approvals", "bridge"))
        self.assertEqual((s.telegram_brief, s.telegram_quiet_start, s.telegram_quiet_end), ((7, 30), (22, 0), (7, 0)))
        self.assertEqual((s.telegram_titles, s.telegram_approvals_hours, s.telegram_approvals_gap_hours,
                          s.telegram_bridge_minutes), (True, 4, 8, 30))

    def test_the_telegram_table_sets_each_value(self):
        s = self.settings('pings = ["bridge", "brief"]\nbrief_time = "06:45"\nquiet_start = "23:00"\n'
                          'quiet_end = "06:30"\ntitles = false\napprovals_hours = 2\napprovals_gap_hours = 12\n'
                          'bridge_minutes = 10\n')
        self.assertEqual(s.telegram_pings, ("bridge", "brief"))
        self.assertEqual((s.telegram_brief, s.telegram_quiet_start, s.telegram_quiet_end), ((6, 45), (23, 0), (6, 30)))
        self.assertEqual((s.telegram_titles, s.telegram_approvals_hours, s.telegram_approvals_gap_hours,
                          s.telegram_bridge_minutes), (False, 2, 12, 10))

    def test_an_empty_list_turns_every_ping_off(self):
        self.assertEqual(self.settings("pings = []\n").telegram_pings, ())

    def test_a_ping_named_twice_counts_once(self):
        self.assertEqual(self.settings('pings = ["brief", "brief", "bridge"]\n').telegram_pings, ("brief", "bridge"))

    def test_values_that_cannot_work_are_refused(self):
        for bad in ('pings = ["brief", "news"]', 'pings = "brief"', 'pings = ""', 'brief_time = "7"', 'quiet_start = "25:00"',
                    'quiet_end = "06:60"', "approvals_hours = -1", "approvals_hours = 73", "approvals_gap_hours = 0",
                    "approvals_gap_hours = 73", "bridge_minutes = -1", "bridge_minutes = 1441"):
            with self.subTest(bad), self.assertRaises(ValueError):
                self.settings(f"{bad}\n")

    def test_the_limits_themselves_are_allowed(self):
        s = self.settings("approvals_hours = 0\napprovals_gap_hours = 1\nbridge_minutes = 0\n")
        self.assertEqual((s.telegram_approvals_hours, s.telegram_approvals_gap_hours, s.telegram_bridge_minutes), (0, 1, 0))
        s = self.settings("approvals_hours = 72\napprovals_gap_hours = 72\nbridge_minutes = 1440\n")
        self.assertEqual((s.telegram_approvals_hours, s.telegram_approvals_gap_hours, s.telegram_bridge_minutes), (72, 72, 1440))


def at(hour: int, minute: int = 0, day: int = 28) -> datetime:
    return datetime(2026, 9, day, hour, minute)


class QuietTests(unittest.TestCase):
    def test_a_window_across_midnight_holds_the_night(self):
        for hour, minute, expected in ((21, 59, False), (22, 0, True), (23, 30, True), (0, 0, True), (6, 59, True),
                                       (7, 0, False), (12, 0, False)):
            with self.subTest(f"{hour:02d}:{minute:02d}"):
                self.assertEqual(quiet(at(hour, minute), (22, 0), (7, 0)), expected)

    def test_a_window_inside_one_day_holds_those_hours(self):
        for hour, minute, expected in ((12, 59, False), (13, 0, True), (14, 59, True), (15, 0, False), (23, 0, False)):
            with self.subTest(f"{hour:02d}:{minute:02d}"):
                self.assertEqual(quiet(at(hour, minute), (13, 0), (15, 0)), expected)

    def test_a_window_that_starts_where_it_ends_is_never_quiet(self):
        for hour in (0, 7, 12, 22):
            self.assertFalse(quiet(at(hour), (7, 0), (7, 0)))


class PingCase(VaultCase):
    """A vault with Telegram on, and a fake bot standing in for Telegram."""
    kinds: tuple[str, ...] | None = None  # the pings this test case asks for; None: the default, all three
    telegram_config = ""
    mail_config = ""

    def extra_config(self) -> str:
        pings = "" if self.kinds is None else "pings = [" + ", ".join(f'"{kind}"' for kind in self.kinds) + "]\n"
        return "[telegram]\nenabled = true\nchat_id = 42\n" + pings + self.telegram_config + self.mail_config

    def setUp(self) -> None:
        super().setUp()
        self.bot = FakeBot()
        self.house = Housekeeper(self.agent)
        for target, value in (("gtd_agent.telegram.get_secret", mock.Mock(return_value="token")),
                              ("gtd_agent.telegram.call", self.bot)):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def ping(self, now: datetime) -> list[str]:
        """One pass of the ping step. `self.bot.texts()` is everything sent so far."""
        return Pings(self.house).run(self.agent.vault(), now)


BRIDGE_CONFIG = ('[mail]\nenabled = true\nsource = "bridge"\nhost = "127.0.0.1"\nport = 1143\n'
                 'user = "sampleowner@example.org"\nown_addresses = ["sampleowner@example.org"]\nfolder = "Labels/GTD"\n')
BRIDGE_DOWN = "Proton Bridge is not answering, so mail is not being read. Open Bridge on the laptop and leave it running."
MAIL_DOWN = "Mail is not being read. TODAY on the laptop says what to fix."
MAIL_BACK = "Mail is being read again."


class BridgeCase(PingCase):
    kinds = ("bridge",)
    mail_config = BRIDGE_CONFIG

    def down(self, closed: bool = True, problem: str = "Can't reach Proton Mail Bridge at 127.0.0.1:1143.") -> None:
        """What the mail step records when it cannot read mail."""
        self.agent.ledger.kv_set("mail_problem", problem)
        self.agent.ledger.kv_set("mail_closed", "1" if closed else "")

    def up(self) -> None:
        self.agent.ledger.kv_set("mail_problem", "")
        self.agent.ledger.kv_set("mail_closed", "")


class BridgeTests(BridgeCase):
    def test_nothing_is_sent_while_mail_works(self):
        self.assertEqual(self.ping(at(9)), [])
        self.assertEqual(self.bot.calls, [])

    def test_a_problem_is_pinged_once_it_has_lasted_the_set_time(self):
        self.down()
        for hour, minute in ((9, 0), (9, 29)):
            self.assertEqual(self.ping(at(hour, minute)), [])
        self.assertEqual(self.bot.texts(), [])
        self.assertEqual(self.ping(at(9, 30)), ["Telegram: told you mail is down"])
        self.assertEqual(self.bot.texts(), [BRIDGE_DOWN])

    def test_it_is_told_once_for_the_whole_outage(self):
        self.down()
        for now in (at(9), at(9, 30), at(10), at(15), at(21)):
            self.ping(now)
        self.assertEqual(self.bot.texts(), [BRIDGE_DOWN])

    def test_you_are_told_when_mail_is_back_and_a_new_outage_counts_from_its_start(self):
        self.down()
        self.ping(at(9))
        self.ping(at(9, 30))
        self.up()
        self.assertEqual(self.ping(at(9, 45)), ["Telegram: told you mail is back"])
        self.assertEqual(self.ping(at(10)), [])
        self.down()
        self.ping(at(10, 15))
        self.ping(at(10, 44))
        self.assertEqual(self.bot.texts(), [BRIDGE_DOWN, MAIL_BACK])
        self.ping(at(10, 45))
        self.assertEqual(self.bot.texts(), [BRIDGE_DOWN, MAIL_BACK, BRIDGE_DOWN])

    def test_an_outage_that_ended_before_you_were_told_stays_quiet(self):
        self.down()
        self.ping(at(9))
        self.up()
        self.ping(at(9, 10))
        self.down()
        self.ping(at(9, 20))
        self.ping(at(9, 49))
        self.assertEqual(self.bot.texts(), [])
        self.ping(at(9, 50))
        self.assertEqual(self.bot.texts(), [BRIDGE_DOWN])

    def test_a_problem_other_than_a_closed_bridge_is_worded_without_its_details(self):
        self.down(closed=False, problem="127.0.0.1 refused the login for sampleowner@example.org. Make a new app password.")
        self.ping(at(9))
        self.ping(at(9, 30))
        self.assertEqual(self.bot.texts(), [MAIL_DOWN])

    def test_quiet_hours_hold_what_is_sent_but_not_the_clock(self):
        self.down()
        self.ping(at(23))            # first seen in the night
        self.assertEqual(self.ping(at(3, 0, 29)), [])
        self.assertEqual(self.bot.texts(), [])
        self.ping(at(7, 0, 29))      # quiet is over: it has been down for hours
        self.assertEqual(self.bot.texts(), [BRIDGE_DOWN])

    def test_mail_that_comes_back_in_the_night_is_told_in_the_morning(self):
        self.down()
        self.ping(at(21))
        self.ping(at(21, 30))
        self.up()
        self.assertEqual(self.ping(at(23)), [])
        self.assertEqual(self.bot.texts(), [BRIDGE_DOWN])
        self.assertEqual(self.ping(at(7, 0, 29)), ["Telegram: told you mail is back"])
        self.assertEqual(self.bot.texts(), [BRIDGE_DOWN, MAIL_BACK])
        self.assertEqual(self.ping(at(8, 0, 29)), [])

    def test_a_back_message_that_could_not_be_sent_is_tried_again(self):
        self.down()
        self.ping(at(9))
        self.ping(at(9, 30))
        self.up()
        self.bot.broken = TelegramError("Telegram request failed (URLError)")
        with self.assertRaises(TelegramError):
            self.ping(at(9, 45))
        self.bot.broken = None
        self.assertEqual(self.ping(at(9, 46)), ["Telegram: told you mail is back"])
        self.assertEqual(self.bot.texts(), [BRIDGE_DOWN, MAIL_BACK])

    def test_a_ping_that_could_not_be_sent_is_tried_again(self):
        self.down()
        self.ping(at(9))
        self.bot.broken = TelegramError("Telegram request failed (URLError)")
        with self.assertRaises(TelegramError):
            self.ping(at(9, 30))
        self.assertEqual((self.bot.texts(), len(self.bot.refused)), ([], 1))
        self.bot.broken = None
        self.assertEqual(self.ping(at(9, 31)), ["Telegram: told you mail is down"])
        self.assertEqual(self.bot.texts(), [BRIDGE_DOWN])


class ImmediateBridgeTests(BridgeCase):
    telegram_config = "bridge_minutes = 0\n"

    def test_the_minutes_are_a_setting(self):
        self.down()
        self.assertEqual(self.ping(at(9)), ["Telegram: told you mail is down"])


def waiting_text(*items: str, total: int | None = None) -> str:
    """What the approvals ping says for these listed items (`total` waiting when more than are listed)."""
    n = total if total is not None else len(items)
    head = "1 thing waits" if n == 1 else f"{n} things wait"
    return "\n".join([f"{head} for your OK in APPROVAL:", *items, "Open APPROVAL to decide."])


class ApprovalCase(PingCase):
    kinds = ("approvals",)

    def propose(self, *lines: str, when: str = "2026-09-28T05:00:00") -> list[str]:
        """Inbox lines become proposals, dated as if they were made at `when`. Their IDs."""
        with mock.patch("gtd_agent.ledger._now", return_value=when):
            self.add_inbox(*lines)
            return [item["id"] for item in self.agent.scan().new_proposals]

    def decide(self, proposal_id: str) -> None:
        self.agent.ledger.set_status(proposal_id, "committed")


class ApprovalTests(ApprovalCase):
    def test_nothing_waiting_sends_nothing(self):
        self.assertEqual(self.ping(at(9)), [])
        self.assertEqual(self.bot.calls, [])

    def test_an_approval_is_pinged_once_it_has_waited_the_set_hours(self):
        self.propose("Buy stamps #errands")  # made at 05:00
        self.assertEqual(self.ping(at(8, 59)), [])
        self.assertEqual(self.ping(at(9)), ["Telegram: told you 1 approval waits"])
        self.assertEqual(self.bot.texts(), [waiting_text("- Next action · Buy stamps")])

    def test_what_waits_is_listed_oldest_first_with_its_kind(self):
        self.propose("Buy stamps #errands", when="2026-09-28T05:00:00")
        self.propose("Call Sam #calls", when="2026-09-28T04:00:00")
        self.ping(at(9))
        self.assertEqual(self.bot.texts(), [waiting_text("- Next action · Call Sam", "- Next action · Buy stamps")])

    def test_an_approval_that_has_not_waited_long_enough_is_not_listed(self):
        self.propose("Buy stamps #errands", when="2026-09-28T05:00:00")
        self.propose("Call Sam #calls", when="2026-09-28T08:00:00")
        self.ping(at(9))
        self.assertEqual(self.bot.texts(), [waiting_text("- Next action · Buy stamps")])

    def test_a_long_list_names_five_and_counts_the_rest(self):
        for n in range(1, 8):
            self.propose(f"Task {n} #errands", when=f"2026-09-28T04:00:{n:02d}")
        self.ping(at(9))
        self.assertEqual(self.bot.texts(), [waiting_text(*(f"- Next action · Task {n}" for n in range(1, 6)),
                                                         "- …and 2 more", total=7)])

    def test_exactly_five_are_all_named_with_no_more_line(self):
        for n in range(1, 6):
            self.propose(f"Task {n} #errands", when=f"2026-09-28T04:00:{n:02d}")
        self.ping(at(9))
        self.assertEqual(self.bot.texts(), [waiting_text(*(f"- Next action · Task {n}" for n in range(1, 6)))])

    def test_a_proposal_made_again_starts_waiting_again(self):
        self.propose("Buy stamps #errands", when="2026-09-28T05:00:00")
        row = self.agent.ledger.pending()[0]
        with mock.patch("gtd_agent.ledger._now", return_value="2026-09-28T08:00:00"):
            self.agent.ledger.upsert(row["origin"], row["dedupe_key"], json.loads(row["proposal_json"]))
        self.assertEqual(self.ping(at(9)), [])        # the new version has waited an hour
        self.assertEqual(self.ping(at(12)), ["Telegram: told you 1 approval waits"])

    def test_a_long_title_is_cut(self):
        self.propose("Buy " + "stamps " * 20 + "#errands")
        self.ping(at(9))
        line = self.bot.texts()[0].splitlines()[1]
        self.assertEqual((len(line), line[-1]), (len("- Next action · ") + 60, "…"))

    def test_private_items_are_never_named(self):
        self.propose("Buy stamps #errands", "Call the dentist [[AREA_ADMIN]] #calls", "Pay the club dues #private #errands")
        self.ping(at(9))
        text = self.bot.texts()[0]
        self.assertEqual(text, waiting_text("- Next action · A private item", "- Next action · A private item",
                                            "- Next action · Buy stamps"))
        for word in ("dentist", "club dues"):
            self.assertNotIn(word, text)

    def test_what_comes_from_outside_is_named_by_kind_only(self):
        with mock.patch("gtd_agent.ledger._now", return_value="2026-09-28T05:00:00"):
            self.agent.ledger.upsert("mail", "mail|abc", {"kind": "mail", "title": "Re: Surgery date", "ops": []})
            self.agent.ledger.upsert("document", "document|x.pdf",
                                     {"kind": "document", "title": "tax-return.pdf", "ops": []})
            self.agent.ledger.upsert("contact", "contact|a@b.c",
                                     {"kind": "contact", "title": "Pat Lee <pat@example.net>", "ops": []})
        self.ping(at(9))
        text = self.bot.texts()[0]
        self.assertEqual(sorted(text.splitlines()[1:4]), ["- Contact", "- Inbox file", "- Mail"])
        for word in ("Surgery", "tax-return", "Pat Lee", "pat@"):
            self.assertNotIn(word, text)

    def test_a_proposal_that_files_into_a_private_project_is_not_named_either(self):
        with mock.patch("gtd_agent.ledger._now", return_value="2026-09-28T05:00:00"):
            self.agent.ledger.upsert("followup", "follow|1", {
                "kind": "follow_up", "title": "Follow up with Riley", "summary": "Waiting",
                "ops": [{"op": "add_task", "path": "02_PROJECTS/TAXES/TAXES.md", "line": "- [ ] Chase Riley"}]})
        self.ping(at(9))
        self.assertEqual(self.bot.texts(), [waiting_text("- Follow-up · A private item")])

    def test_with_titles_off_only_the_count_is_sent(self):
        self.agent.settings = dataclasses.replace(self.settings, telegram_titles=False)
        self.house.settings = self.agent.settings
        self.propose("Buy stamps #errands", "Call Sam #calls")
        self.ping(at(9))
        self.assertEqual(self.bot.texts(), ["2 things wait for your OK in APPROVAL. Open it to decide."])

    def test_one_thing_with_titles_off_is_singular(self):
        self.agent.settings = dataclasses.replace(self.settings, telegram_titles=False)
        self.house.settings = self.agent.settings
        self.propose("Buy stamps #errands")
        self.ping(at(9))
        self.assertEqual(self.bot.texts(), ["1 thing waits for your OK in APPROVAL. Open it to decide."])


class ApprovalNamingTests(ApprovalCase):
    """Which proposals the ping names, and how. A ping never names anything private."""

    def named(self, proposal: dict, origin: str = "inbox") -> str:
        """The line the ping gives for this one proposal, once it has waited."""
        with mock.patch("gtd_agent.ledger._now", return_value="2026-09-28T05:00:00"):
            self.agent.ledger.upsert(origin, "k|1", {"ops": [], **proposal})
        self.ping(at(9))
        return self.bot.texts()[0].splitlines()[1]

    def test_a_proposal_marked_private_is_not_named(self):
        self.assertEqual(self.named({"kind": "next_action", "title": "Secret thing", "private": True}),
                         "- Next action · A private item")

    def test_a_private_tag_deep_inside_a_proposal_hides_it(self):
        proposal = {"kind": "manual", "title": "Plain words", "ops": [
            {"op": "add_task", "path": "01_GTD/SINGLE_ACTIONS.md", "detail": {"lines": ["- [ ] Pay rent #private"]}}]}
        self.assertEqual(self.named(proposal), "- Needs clarification · A private item")

    def test_a_link_to_a_private_area_hides_it(self):
        self.assertEqual(self.named({"kind": "manual", "title": "Plain words", "summary": "Filed under [[AREA_ADMIN]]"}),
                         "- Needs clarification · A private item")

    def test_filing_into_a_private_area_note_hides_it(self):
        op = {"op": "append_log", "path": "03_AREAS/AREA_ADMIN.md", "entry": "Plain words"}
        self.assertEqual(self.named({"kind": "project_note", "title": "Plain words", "ops": [op]}),
                         "- Project log · A private item")

    def test_filing_into_a_project_that_is_not_private_leaves_it_named(self):
        op = {"op": "append_log", "path": "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md", "entry": "Plain words"}
        self.assertEqual(self.named({"kind": "project_note", "title": "Plain words", "ops": [op]}),
                         "- Project log · Plain words")

    def test_a_kind_without_a_label_is_worded_from_its_name(self):
        self.assertEqual(self.named({"kind": "brand_new_kind", "title": "Plain words"}), "- Brand new kind · Plain words")

    def test_a_proposal_without_a_title_is_named_by_its_source(self):
        self.assertEqual(self.named({"kind": "next_action", "source": "Call Sam #calls"}),
                         "- Next action · Call Sam #calls")

    def test_words_are_kept_as_typed_and_on_one_line(self):
        self.assertEqual(self.named({"kind": "next_action", "title": "Call <Sam> & Pat,\n  re: the lab"}),
                         "- Next action · Call <Sam> & Pat, re: the lab")

    def test_a_proposal_with_no_words_is_named_by_its_kind_alone(self):
        self.assertEqual(self.named({"kind": "next_action"}), "- Next action")

    def test_a_proposal_whose_steps_are_not_the_usual_shape_is_still_named(self):
        self.assertEqual(self.named({"kind": "next_action", "title": "Plain words", "ops": ["oops", 7]}),
                         "- Next action · Plain words")

    def test_a_title_of_exactly_the_limit_is_kept_whole(self):
        self.assertEqual(self.named({"kind": "next_action", "title": "x" * 60}), "- Next action · " + "x" * 60)

    def test_a_title_one_over_the_limit_is_cut(self):
        self.assertEqual(self.named({"kind": "next_action", "title": "x" * 61}), "- Next action · " + "x" * 59 + "…")


class ApprovalRhythmTests(ApprovalCase):
    """After a ping, a new approval is told after the gap; with nothing new, the reminder comes once a day."""

    def first(self) -> list[str]:
        ids = self.propose("Buy stamps #errands")
        self.ping(at(9))
        self.assertEqual(len(self.bot.texts()), 1)
        return ids

    def test_a_new_approval_waits_for_the_gap_since_the_last_ping(self):
        self.first()
        self.propose("Call Sam #calls", when="2026-09-28T09:30:00")
        self.assertEqual(self.ping(at(13, 30)), [])       # waited 4 hours, but the last ping was 4.5 hours ago
        self.assertEqual(self.ping(at(16, 59)), [])
        self.assertEqual(self.ping(at(17)), ["Telegram: told you 2 approvals wait"])
        self.assertEqual(self.bot.texts()[1], waiting_text("- Next action · Buy stamps", "- Next action · Call Sam"))

    def test_with_nothing_new_the_reminder_comes_once_a_day(self):
        self.first()
        self.assertEqual(self.ping(at(17)), [])
        self.assertEqual(self.ping(at(8, 59, 29)), [])
        self.assertEqual(self.ping(at(9, 0, 29)), ["Telegram: told you 1 approval waits"])
        self.assertEqual(self.ping(at(17, 0, 29)), [])
        self.assertEqual(len(self.bot.texts()), 2)

    def test_what_you_decided_is_not_listed_again(self):
        ids = self.first()
        self.decide(ids[0])
        self.propose("Call Sam #calls", when="2026-09-28T09:30:00")
        self.ping(at(17))
        self.assertEqual(self.bot.texts()[1], waiting_text("- Next action · Call Sam"))

    def test_a_proposal_made_again_under_a_new_id_is_not_new(self):
        self.first()
        row = self.agent.ledger.pending()[0]
        with mock.patch("gtd_agent.ledger._now", return_value="2026-09-28T10:00:00"):
            self.agent.ledger.upsert(row["origin"], row["dedupe_key"], json.loads(row["proposal_json"]))
        self.assertEqual(self.ping(at(17)), [])  # waited 7 hours since it was remade; nothing about it is new

    def test_the_gap_is_a_setting(self):
        self.agent.settings = dataclasses.replace(self.settings, telegram_approvals_gap_hours=1)
        self.house.settings = self.agent.settings
        self.first()
        self.propose("Call Sam #calls", when="2026-09-28T05:30:00")
        self.assertEqual(self.ping(at(9, 59)), [])
        self.assertEqual(self.ping(at(10)), ["Telegram: told you 2 approvals wait"])

    def test_quiet_hours_hold_the_ping(self):
        self.propose("Buy stamps #errands", when="2026-09-28T18:00:00")
        self.assertEqual(self.ping(at(22, 30)), [])
        self.assertEqual(self.ping(at(3, 0, 29)), [])
        self.assertEqual(self.bot.texts(), [])
        self.assertEqual(self.ping(at(7, 0, 29)), ["Telegram: told you 1 approval waits"])

    def test_a_ping_that_could_not_be_sent_is_tried_again(self):
        self.propose("Buy stamps #errands")
        self.bot.broken = TelegramError("Telegram request failed (URLError)")
        with self.assertRaises(TelegramError):
            self.ping(at(9))
        self.bot.broken = None
        self.assertEqual(self.ping(at(9, 1)), ["Telegram: told you 1 approval waits"])
        self.assertEqual(len(self.bot.texts()), 1)


class ImmediateApprovalTests(ApprovalCase):
    telegram_config = "approvals_hours = 0\n"

    def test_the_hours_are_a_setting(self):
        self.propose("Buy stamps #errands", when="2026-09-28T09:00:00")
        self.assertEqual(self.ping(at(9)), ["Telegram: told you 1 approval waits"])


class OnlyApprovalsAskedTests(ApprovalCase):
    kinds = ("bridge",)  # mail is off, so there is nothing to say

    def test_a_ping_you_left_out_of_the_list_is_not_sent(self):
        self.propose("Buy stamps #errands")
        self.ping(at(9))
        self.assertEqual(self.bot.calls, [])


class BriefCase(PlanCase):
    """The standard planning day (test_plan): Monday 28 September, three events, three planned blocks. The brief is
    sent by the same housekeeping check that makes the plan, so `keep` is all a test needs."""
    telegram_config = ""

    def extra_config(self) -> str:
        return super().extra_config() + "[telegram]\nenabled = true\nchat_id = 42\n" + self.telegram_config

    def setUp(self) -> None:
        super().setUp()
        self.bot = FakeBot()
        for target, value in (("gtd_agent.telegram.get_secret", mock.Mock(return_value="token")),
                              ("gtd_agent.telegram.call", self.bot)):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)


HEADING = "Mon Sep 28"
CALENDAR_BLOCK = "Calendar\n09:00–10:00 Stand-up\n12:00–13:00 Lunch\n15:00–16:00 Dentist"
PLAN_BLOCK = ("Plan\n10:00–10:30 Pay parking ticket · 3 days overdue\n10:30–10:45 Renew library card · due today\n"
              "13:00–15:00 Draft the budget · due Wed Sep 30")
NEEDS = "Due or overdue: 2 · Follow-ups due: 2 · APPROVAL: 2"  # the two overdue waits have follow-up drafts
LOAD = "2h 45m of work, 94h free this week. Counted 1 task at 30m: no time tag."
BRIEF = "\n\n".join([HEADING, CALENDAR_BLOCK, PLAN_BLOCK, f"{NEEDS}\n{LOAD}"])


class BriefTests(BriefCase):
    def test_the_brief_names_the_day_the_calendar_the_plan_and_what_needs_you(self):
        self.keep()
        self.assertEqual(self.bot.texts(), [BRIEF])

    def test_it_is_sent_once_a_day_and_again_the_next(self):
        self.keep()
        self.keep(NOW + timedelta(minutes=5))
        self.assertEqual(len(self.bot.texts()), 1)
        self.agent._today = date(2026, 9, 29)
        self.keep(datetime(2026, 9, 29, 9, 0))
        self.assertEqual(len(self.bot.texts()), 2)
        self.assertTrue(self.bot.texts()[1].startswith("Tue Sep 29\n\nCalendar: nothing scheduled"))

    def test_the_run_says_what_it_sent(self):
        self.assertIn("Telegram: sent the morning brief", self.keep())

    def test_it_waits_for_its_time(self):
        self.keep(at(7, 29))
        self.assertEqual(self.bot.texts(), [])
        self.keep(at(7, 30))
        self.assertEqual(len(self.bot.texts()), 1)

    def test_a_laptop_that_wakes_long_after_the_morning_gets_no_brief(self):
        self.keep(at(13, 30))   # six hours after its time
        self.assertEqual(self.bot.texts(), [])

    def test_a_laptop_that_wakes_within_the_morning_still_does(self):
        self.keep(at(13, 29))
        self.assertEqual(len(self.bot.texts()), 1)

    def test_a_brief_that_could_not_be_sent_is_tried_again_and_the_check_goes_on(self):
        self.bot.broken = TelegramError("Telegram request failed (URLError)")
        self.keep()
        self.assertEqual(self.bot.texts(), [])
        self.assertEqual(self.agent.health.snapshot()["last_error"]["label"], "Housekeeping › phone pings")
        self.assertTrue(self.path("_agent/TODAY.md").exists())           # the rest of the check ran
        self.bot.broken = None
        self.keep(NOW + timedelta(minutes=1))
        self.assertEqual(self.bot.texts(), [BRIEF])

    def test_the_approvals_ping_leaves_what_the_brief_already_told(self):
        with mock.patch("gtd_agent.ledger._now", return_value="2026-09-28T01:00:00"):
            self.add_inbox("Buy stamps #errands")
            self.agent.scan()
        self.keep()
        texts = self.bot.texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("APPROVAL: 3", texts[0])      # the two follow-up drafts and the new line
        self.keep(NOW + timedelta(minutes=10))
        self.assertEqual(len(self.bot.texts()), 1)


def ics_event(uid: str, summary: str, start: str, end: str, extra: str = "") -> str:
    """An event with extra property lines (CLASS:PRIVATE, DTSTART parameters...)."""
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{summary}\r\nDTSTART{extra}:{start}\r\nDTEND{extra}:{end}\r\n"
            "END:VEVENT\r\n")


class BriefPrivacyTests(BriefCase):
    """A private task, a private event: the brief says there is one, and nothing more."""
    ics = calendar(
        event("standup", "Stand-up", "20260928T090000", "20260928T100000"),
        "BEGIN:VEVENT\r\nUID:checkup\r\nSUMMARY:Checkup with Dr Lee\r\nDTSTART:20260928T120000\r\n"
        "DTEND:20260928T130000\r\nCLASS:PRIVATE\r\nEND:VEVENT\r\n")

    def extra_files(self) -> dict[str, str]:
        files = super().extra_files()
        files[SINGLE] = SINGLE_TEXT.replace("Call the dentist [[AREA_ADMIN]] #calls #15m",
                                            "Call the dentist [[AREA_ADMIN]] #calls #15m 📅 2026-09-20")
        return files

    def test_a_private_event_and_a_private_task_are_not_named(self):
        self.keep()
        text = self.bot.texts()[0]
        self.assertIn("12:00–13:00 A private event", text)
        self.assertIn("10:00–10:15 A private task · 8 days overdue", text)
        for word in ("Checkup", "Dr Lee", "dentist"):
            self.assertNotIn(word, text)
        self.assertIn("09:00–10:00 Stand-up", text)            # what is not private is still named


class BriefWordsTests(BriefCase):
    ics = calendar(event("odd", "Call <Sam> &amp; Pat\\, re: the lab", "20260928T090000", "20260928T100000"))

    def test_words_are_cut_to_a_line_and_not_escaped_for_markdown(self):
        self.keep()
        self.assertIn("09:00–10:00 Call <Sam> &amp; Pat, re: the lab", self.bot.texts()[0])


class BriefLineTests(BriefCase):
    ics = calendar(event("wrapped", "Lab\\nsetup   (room 4)", "20260928T090000", "20260928T100000"))

    def test_a_title_on_two_lines_or_with_extra_spaces_is_one_line(self):
        self.keep()
        self.assertIn("Calendar\n09:00–10:00 Lab setup (room 4)\n", self.bot.texts()[0])


class BriefCalendarTests(BriefCase):
    def brief_of(self, ics: str, now: datetime = NOW) -> str:
        self.calendar(ics)
        self.keep(now)
        return self.bot.texts()[0]

    def test_an_empty_day_says_so(self):
        text = self.brief_of(calendar())
        self.assertIn("Mon Sep 28\n\nCalendar: nothing scheduled\n\nPlan\n09:00–09:30 Pay parking ticket", text)

    def test_all_day_events_come_first(self):
        text = self.brief_of(calendar(event("lunch", "Lunch", "20260928T120000", "20260928T130000"),
                                      event("holiday", "Founders Day", "20260928", "20260929", ";VALUE=DATE")))
        self.assertIn("Calendar\nall day Founders Day\n12:00–13:00 Lunch\n\nPlan", text)

    def test_all_day_events_come_before_one_that_began_the_night_before(self):
        text = self.brief_of(calendar(event("night", "Night shift", "20260927T220000", "20260928T020000"),
                                      event("holiday", "Founders Day", "20260928", "20260929", ";VALUE=DATE")))
        self.assertIn("Calendar\nall day Founders Day\nuntil 02:00 Night shift\n\nPlan", text)

    def test_an_event_that_runs_past_midnight_says_when_it_ends(self):
        text = self.brief_of(calendar(event("flight", "Night flight", "20260928T230000", "20260929T010000")))
        self.assertIn("Calendar\n23:00 until Tue 01:00 Night flight\n", text)

    def test_a_long_day_names_eight_events_and_counts_the_rest(self):
        text = self.brief_of(calendar(*(event(f"e{n}", f"Meeting {n}", f"20260928T{9 + n // 2:02d}{30 * (n % 2):02d}00",
                                             f"20260928T{9 + n // 2:02d}{30 * (n % 2) + 15:02d}00") for n in range(11))))
        block = text.split("\n\n")[1].splitlines()
        self.assertEqual(block[0], "Calendar")
        self.assertEqual(block[1:], [f"{9 + n // 2:02d}:{30 * (n % 2):02d}–{9 + n // 2:02d}:{30 * (n % 2) + 15:02d} Meeting {n}"
                                      for n in range(8)] + ["…and 3 more"])

    def test_exactly_eight_events_are_all_named(self):
        text = self.brief_of(calendar(*(event(f"e{n}", f"Meeting {n}", f"20260928T{9 + n:02d}0000", f"20260928T{9 + n:02d}1500")
                                        for n in range(8))))
        self.assertNotIn("more", text.split("\n\n")[1])



class BriefOneEventTitlesOffTests(BriefCase):
    telegram_config = "titles = false\n"
    ics = calendar(event("lunch", "Lunch", "20260928T120000", "20260928T130000"))

    def test_one_event_is_singular(self):
        self.keep()
        self.assertIn("\n\nCalendar: 1 event\n\n", self.bot.texts()[0])


class BriefCalendarUnreadableTests(BriefCase):
    def setUp(self) -> None:
        super().setUp()
        (self.settings.state_dir / "calendar" / "cache.ics").unlink()

    def test_a_calendar_that_cannot_be_read_is_said_to_be_unavailable_and_the_tasks_come_untimed(self):
        with mock.patch("gtd_agent.housekeeping.fetch_ics", return_value=(None, "Calendar unavailable (URLError)")):
            self.keep()
        self.assertEqual(self.bot.texts(), ["\n\n".join([
            HEADING, "Calendar: unavailable",
            "Top tasks\nPay parking ticket · 3 days overdue\nRenew library card · due today\n"
            "Draft the budget · due Wed Sep 30",
            f"{NEEDS}\n2h 45m of work this week. Free time unknown: calendar unavailable. Counted 1 task at 30m: no time tag."])])


class BriefNoCalendarTests(VaultCase):
    """Without a calendar there is no calendar block, and the top tasks have no times."""

    def extra_config(self) -> str:
        return "[telegram]\nenabled = true\nchat_id = 42\n"

    def extra_files(self) -> dict[str, str]:
        return {SINGLE: SINGLE_TEXT, LAB: LAB_TEXT, TAXES: TAXES_TEXT}

    def test_the_brief_leaves_the_calendar_out(self):
        bot = FakeBot()
        with mock.patch("gtd_agent.telegram.get_secret", return_value="token"), mock.patch("gtd_agent.telegram.call", bot):
            Housekeeper(self.agent).run(now=NOW)
        self.assertEqual(bot.texts(), ["\n\n".join([
            HEADING,
            "Top tasks\nPay parking ticket · 3 days overdue\nRenew library card · due today\n"
            "Draft the budget · due Wed Sep 30",
            f"{NEEDS}\n2h 45m of work this week. Free time unknown: calendar off. Counted 1 task at 30m: no time tag."])])


class BriefTimeTests(BriefCase):
    telegram_config = 'brief_time = "08:15"\n'

    def test_the_brief_time_is_a_setting(self):
        self.keep(at(8, 14))
        self.assertEqual(self.bot.texts(), [])
        self.keep(at(8, 15))
        self.assertEqual(len(self.bot.texts()), 1)


class BriefNoRoomTests(BriefCase):
    plan_config = 'day_end = "10:00"\n'      # the stand-up fills the only hour the day has left

    def test_a_day_with_no_free_gap_says_the_top_tasks_fit_nowhere(self):
        self.keep()
        self.assertEqual(self.bot.texts()[0].split("\n\n")[2], "Plan: no free gap fits the top tasks")


class BriefNoRoomTitlesOffTests(BriefNoRoomTests):
    telegram_config = "titles = false\n"


class BriefNothingToPlanTests(BriefCase):
    def extra_files(self) -> dict[str, str]:
        empty = "---\nkey: {0}\ntype: project\nstatus: active\narea: \"[[AREA_CLUB]]\"\n---\n# {0}\n\n## Steps\n"
        return {SINGLE: "# SINGLE_ACTIONS\n\n## @computer\n\n## Waiting For\n",
                LAB: empty.format("DEMO_PROJECT"), TAXES: empty.format("TAXES")}

    def test_a_day_with_nothing_to_place_has_no_plan_block(self):
        self.keep()
        blocks = self.bot.texts()[0].split("\n\n")
        self.assertEqual(blocks[:2], [HEADING, CALENDAR_BLOCK])
        self.assertEqual(len(blocks), 3)         # the heading, the calendar, then what needs you and the load


class BriefOffDayTests(BriefCase):
    plan_config = '[plan]\ndays = ["tue", "wed"]\n'

    def test_a_day_without_a_plan_has_the_calendar_and_no_plan_block(self):
        self.keep()
        text = self.bot.texts()[0]
        self.assertEqual(text.split("\n\n")[:2], [HEADING, CALENDAR_BLOCK])
        self.assertNotIn("Plan", text)


class BriefLengthTests(BriefCase):
    ics = calendar(*(event(f"e{n}", "Long " * 80, f"20260928T{9 + n // 4:02d}{15 * (n % 4):02d}00",
                           f"20260928T{9 + n // 4:02d}{15 * (n % 4) + 10:02d}00") for n in range(40)))

    def test_a_crowded_day_still_fits_in_one_message(self):
        self.keep()
        self.assertLess(len(self.bot.texts()[0]), 4096)
        self.assertNotIn("Long " * 20, self.bot.texts()[0])


class BriefOneBlockTitlesOffTests(BriefCase):
    telegram_config = "titles = false\n"
    plan_config = "[plan]\ntasks = 1\n"

    def test_one_block_is_singular(self):
        self.keep()
        self.assertIn("\n\nPlan: 1 block, first at 10:00\n\n", self.bot.texts()[0])


class BriefUntimedTitlesOffTests(VaultCase):
    plan_config = ""
    counted = "Plan: 3 top tasks"

    def extra_config(self) -> str:
        return "[telegram]\nenabled = true\nchat_id = 42\ntitles = false\n" + self.plan_config

    def extra_files(self) -> dict[str, str]:
        return {SINGLE: SINGLE_TEXT, LAB: LAB_TEXT, TAXES: TAXES_TEXT}

    def test_tasks_without_times_are_counted(self):
        bot = FakeBot()
        with mock.patch("gtd_agent.telegram.get_secret", return_value="token"), mock.patch("gtd_agent.telegram.call", bot):
            Housekeeper(self.agent).run(now=NOW)
        self.assertIn(f"\n\n{self.counted}\n\n", bot.texts()[0])
        self.assertNotIn("Calendar", bot.texts()[0])


class BriefUntimedOneTaskTitlesOffTests(BriefUntimedTitlesOffTests):
    plan_config = "[plan]\ntasks = 1\n"
    counted = "Plan: 1 top task"


class BriefQuietDayTests(VaultCase):
    """Nothing due, no waits, nothing to approve."""

    def extra_config(self) -> str:
        return "[telegram]\nenabled = true\nchat_id = 42\n"

    def extra_files(self) -> dict[str, str]:
        undated = lambda text: re.sub(r" 📅 \d{4}-\d{2}-\d{2}", "", text).replace("#waiting", "#someday #waiting")
        return {SINGLE: undated(SINGLE_TEXT).split("## Waiting For")[0], LAB: undated(LAB_TEXT).split("## Waiting For")[0],
                TAXES: TAXES_TEXT}

    def test_it_says_that_nothing_is_due_or_waiting(self):
        bot = FakeBot()
        with mock.patch("gtd_agent.telegram.get_secret", return_value="token"), mock.patch("gtd_agent.telegram.call", bot):
            Housekeeper(self.agent).run(now=NOW)
        self.assertEqual(bot.texts()[0].split("\n\n")[-1].splitlines()[0], "Nothing is due or waiting.")


class BriefWithApprovalsAndInboxTests(BriefCase):
    def test_what_waits_for_you_is_counted(self):
        self.add_inbox("Buy stamps #errands", "Call Sam #calls")
        self.agent.scan()
        self.add_inbox("A line not yet read")
        self.keep()
        self.assertIn(f"Due or overdue: 2 · Follow-ups due: 2 · APPROVAL: 4 · Inbox: 3\n{LOAD}",
                      self.bot.texts()[0])  # the two lines you added are in the Inbox and in APPROVAL


class BriefThenApprovalsTests(ApprovalCase):
    """The brief counts what waits in APPROVAL, so the approvals pings that follow tell only what is new."""
    kinds = ("brief", "approvals")

    def test_what_the_brief_counted_is_not_told_again_as_new(self):
        self.propose("Buy stamps #errands", when="2026-09-28T01:00:00")
        self.ping(at(8))
        self.assertEqual(len(self.bot.texts()), 1)                        # the brief
        self.assertEqual(self.ping(at(17)), [])                           # past the gap, and nothing is new
        self.assertEqual(self.ping(at(8, 0, 29)), ["Telegram: told you 1 approval waits"])   # the daily reminder

    def test_a_brief_with_nothing_waiting_does_not_hold_back_the_first_approvals_ping(self):
        self.ping(at(8))
        self.assertEqual(len(self.bot.texts()), 1)
        self.propose("Buy stamps #errands", when="2026-09-28T08:10:00")
        self.assertEqual(self.ping(at(12, 10)), ["Telegram: told you 1 approval waits"])


class BriefQuietTests(BriefCase):
    telegram_config = 'brief_time = "06:00"\nquiet_end = "08:00"\n'

    def test_quiet_hours_hold_the_brief_until_they_end(self):
        self.keep(at(7, 30))
        self.assertEqual(self.bot.texts(), [])
        self.keep(at(8, 0))
        self.assertEqual(len(self.bot.texts()), 1)


class BriefTitlesOffTests(BriefCase):
    telegram_config = "titles = false\n"

    def test_the_brief_gives_counts_and_no_names(self):
        self.keep()
        self.assertEqual(self.bot.texts(), ["\n\n".join([HEADING, "Calendar: 3 events",
                                                          "Plan: 3 blocks, first at 10:00", f"{NEEDS}\n{LOAD}"])])


class OnlyBriefAskedTests(BriefCase):
    telegram_config = 'pings = ["bridge"]\n'  # mail is off here, so there is nothing for it to say

    def test_a_ping_you_left_out_of_the_list_is_not_sent(self):
        self.keep()
        self.assertEqual(self.bot.calls, [])


class OnlyBridgeAskedTests(BridgeCase):
    kinds = ("approvals",)  # nothing waits, so there is nothing to say

    def test_a_ping_you_left_out_of_the_list_is_not_sent(self):
        self.down()
        self.ping(at(9))
        self.ping(at(15))
        self.assertEqual(self.bot.calls, [])


class NoPingsTests(BridgeCase):
    kinds = ()

    def test_an_empty_list_leaves_telegram_to_capture(self):
        self.down()
        self.ping(at(9))
        self.ping(at(15))
        self.assertEqual(self.bot.calls, [])


class NotReadyTests(BridgeCase):
    """Without the switch, the chat or the token there is nobody to write to: the step does nothing, and says nothing
    (the capture step already reports a missing token or chat)."""
    telegram_config = "bridge_minutes = 0\n"

    def test_no_token_means_no_ping(self):
        self.down()
        with mock.patch("gtd_agent.telegram.get_secret", return_value=None):
            self.assertEqual(self.ping(at(9)), [])
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(self.ping(at(9, 1)), ["Telegram: told you mail is down"])  # the token came back

    def test_no_chat_means_no_ping(self):
        self.down()
        self.house.settings = dataclasses.replace(self.settings, telegram_chat_id=0)
        self.agent.settings = self.house.settings
        self.assertEqual(self.ping(at(9)), [])
        self.assertEqual(self.bot.calls, [])


class SwitchedOffTests(VaultCase):
    def extra_config(self) -> str:
        return "[telegram]\nenabled = false\nchat_id = 42\n" + BRIDGE_CONFIG

    def test_telegram_that_is_off_sends_nothing(self):
        bot = FakeBot()
        self.agent.ledger.kv_set("mail_problem", "Can't reach Proton Mail Bridge at 127.0.0.1:1143.")
        with mock.patch("gtd_agent.telegram.get_secret", return_value="token"), mock.patch("gtd_agent.telegram.call", bot):
            self.assertEqual(Pings(Housekeeper(self.agent)).run(self.agent.vault(), at(9)), [])
            self.assertEqual(Pings(Housekeeper(self.agent)).run(self.agent.vault(), at(15)), [])
        self.assertEqual(bot.calls, [])


class MailOffTests(PingCase):
    kinds = ("bridge",)

    def test_mail_that_is_switched_off_is_never_pinged(self):
        self.agent.ledger.kv_set("mail_problem", "Can't reach Proton Mail Bridge at 127.0.0.1:1143.")
        self.ping(at(9))
        self.ping(at(15))
        self.assertEqual(self.bot.calls, [])


class FoundByTheMailStepTests(BridgeCase):
    """The mail step is what records a closed Bridge: nothing answers on the port, and the ping says Bridge."""

    def setUp(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.mail_config = BRIDGE_CONFIG.replace("1143", str(self.port))
        patcher = mock.patch.dict(os.environ, {"GTD_SECRET_AGENT_MAIL": "bridge-password"})
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_a_bridge_that_is_closed_is_pinged_as_bridge(self):
        self.house.run(now=at(9))
        self.assertIn("Can't reach Proton Mail Bridge", self.agent.ledger.kv_get("mail_problem"))
        self.assertEqual(self.bot.texts(), [])
        self.house.run(now=at(9, 40))
        self.assertEqual(self.bot.texts(), [BRIDGE_DOWN])


if __name__ == "__main__":
    unittest.main()
