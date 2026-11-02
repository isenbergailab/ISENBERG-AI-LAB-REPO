"""The trust report (phase 3 item 4): in the weekly review, per kind of proposal, how many were proposed, approved as
written, edited first and rejected that week, and which kinds earned auto-apply. It only suggests: config.toml
stays SampleOwner's."""
from __future__ import annotations

import io
import json
import unittest
import uuid
from contextlib import redirect_stdout
from datetime import date, datetime
from unittest import mock

from helpers import TODAY, VaultCase, draft
from gtd_agent.cli import main
from gtd_agent.feedback import with_feedback
from gtd_agent.housekeeping import Housekeeper
from gtd_agent.workflow import Agent

REVIEW = "01_GTD/REVIEWS/2026-W40.md"  # the week of Monday 2026-09-28
LAB = "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"
CLUB = "03_AREAS/AREA_CLUB.md"
ITEM_END = "<!-- gtd-agent:item-end -->"
DECISIONS = "# DEMO_PROJECT · Decisions\n\n← [[DEMO_PROJECT]]\n\n## Budget\n- **No dues** · free · 2026-09-21\n\n" \
            "## Open decisions\n- [ ] Who advises the lab?\n- Which room do we book?\n"
SUNDAY = datetime(2026, 10, 4, 9, 0)
HEAD = ["This week, per kind: proposed, approved as written, edited first, rejected, and applied on their own (auto).",
        "| Kind | Proposed | Approved | Edited | Rejected | Auto |",
        "|---|---|---|---|---|---|"]
NONE_YET = ("- Nothing new has earned auto-apply: it takes 8 approvals as written on 3 different days in the last 28, "
            "none edited or rejected.")
DAYS = ("2026-09-29", "2026-10-01", "2026-10-03")
HOW_TO = ("- To turn one on, add its name to `bookkeeping` under `[trust]` in config.toml, then run `run.ps1 restart`. "
          "What it applies is logged and can be undone.")


def trust_of(note: str) -> list[str]:
    """The lines under '### Trust' in a weekly review."""
    section = note.split("### Trust\n", 1)[1].split("\n### ", 1)[0]
    return [line for line in section.splitlines() if line.strip()]


def row(label: str, *counts: int) -> str:
    return "| " + " | ".join([label, *map(str, counts)]) + " |"


class TrustCase(VaultCase):
    remote = True

    def clock(self, day: str, hour: int = 10):
        """Everything the ledger records inside this block is dated then."""
        return mock.patch("gtd_agent.ledger._now", return_value=f"{day}T{hour:02d}:00:00")

    def capture(self, *lines: str, day: str = "2026-09-29") -> list[str]:
        """Propose each line on that day; the IDs of the proposals."""
        with self.clock(day):
            self.add_inbox(*lines)
            return [item["id"] for item in self.agent.scan().new_proposals]

    def decide(self, how: str, proposal_id: str, day: str = "2026-09-30") -> None:
        with self.clock(day):
            getattr(self.agent, how)(proposal_id)

    def review(self, now: datetime = SUNDAY) -> list[str]:
        Housekeeper(self.agent).weekly(self.agent.vault(), now, force=True)
        return trust_of(self.read(REVIEW))

    def settle(self, kind: str, count: int, status: str = "committed", day: str = "2026-10-01", **more) -> list[str]:
        """Ledger rows as if you had decided them: one proposal of this kind per count, all on this day."""
        with self.clock(day):
            return [self.agent.ledger.upsert("inbox", f"{kind}|{uuid.uuid4().hex}", {"kind": kind, **more},
                                             status=status) for _ in range(count)]

    def spread(self, kind: str, count: int, days: tuple[str, ...] = DAYS, **more) -> list[str]:
        """Approvals as written, dealt out over these days."""
        return [proposal_id for n in range(count) for proposal_id in self.settle(kind, 1, day=days[n % len(days)], **more)]

    def suggestion(self, label: str, kind: str, count: int, days: int = 3) -> str:
        return (f"- Suggest auto-apply: {label} (`{kind}`) · {count} approved as written on {days} different days in "
                "the last 28, none edited or rejected")

    def take_back(self, ids: list[str]) -> None:
        for proposal_id in ids:
            self.agent.ledger.undo_status(self.agent.ledger.add_undo("proposal", proposal_id, "x", []), "undone")


class TableTests(TrustCase):
    def test_a_week_with_nothing_to_count_says_so(self):
        self.assertEqual(self.review(), ["- Nothing proposed or decided this week."])

    def test_each_kind_gets_a_row_of_what_you_decided(self):
        plants, dentist, stamps = self.capture("Water the plants #anywhere", "Call the dentist #calls",
                                               "Buy stamps #errands")
        self.decide("approve", plants)
        self.decide("approve", dentist)
        self.decide("reject", stamps)
        self.assertEqual(self.review(), [*HEAD, row("Next action", 3, 2, 0, 1, 0), NONE_YET])


class WindowTests(TrustCase):
    """The week runs Monday to Sunday: a proposal counts where it was made, a decision where it was taken."""

    def test_proposals_count_in_the_week_they_were_made(self):
        for day in ("2026-09-27", "2026-09-28", "2026-10-04", "2026-10-05"):  # Sunday before ... Monday after
            self.capture(f"Water the plants on {day} #anywhere", day=day)
        self.assertEqual(self.review(), [*HEAD, row("Next action", 2, 0, 0, 0, 0), NONE_YET])

    def test_decisions_count_in_the_week_they_were_taken(self):
        ids = self.capture("Water the plants #anywhere", "Call the dentist #calls", "Buy stamps #errands",
                           "Mail the form #errands")
        for proposal_id, day in zip(ids, ("2026-09-27", "2026-09-28", "2026-10-04", "2026-10-05")):
            self.decide("approve", proposal_id, day)
        self.assertEqual(self.review(), [*HEAD, row("Next action", 4, 2, 0, 0, 0), NONE_YET])

    def test_a_rebuilt_proposal_is_counted_once(self):
        first = self.capture("Water the plants #anywhere")
        with self.clock("2026-10-01"):
            self.agent.scan()  # nothing changed: the same proposal stays
        self.assertEqual(self.review(), [*HEAD, row("Next action", 1, 0, 0, 0, 0), NONE_YET])
        self.assertEqual(len(first), 1)

    def test_a_proposal_still_waiting_is_only_proposed(self):
        self.capture("Water the plants #anywhere")
        self.assertEqual(self.review(), [*HEAD, row("Next action", 1, 0, 0, 0, 0), NONE_YET])


class SetAsideTests(TrustCase):
    def test_setting_aside_or_skipping_is_a_no(self):
        a, b, c = self.capture("Water the plants #anywhere", "Call the dentist #calls", "Buy stamps #errands")
        with self.clock("2026-09-30"):
            self.agent.reject(a, status="set_aside")
            self.agent.reject(b, status="skipped")
            self.agent.reject(c, status="left")
        self.assertEqual(self.review(), [*HEAD, row("Next action", 3, 0, 0, 3, 0), NONE_YET])


class OpenTests(TrustCase):
    """Only a settled proposal is a decision."""

    def test_open_stale_and_in_flight_proposals_are_only_proposed(self):
        for status in ("pending", "applying", "stale"):
            self.settle("next_action", 1, status=status)
        self.assertEqual(self.review(), [*HEAD, row("Next action", 3, 0, 0, 0, 0), NONE_YET])

    def test_an_old_proposal_touched_this_week_is_not_a_row(self):
        (old,) = self.settle("next_action", 1, status="pending", day="2026-08-01")
        with self.clock("2026-10-01"):
            self.agent.ledger.update_json(old, {"kind": "next_action", "note": "refreshed"})
        self.assertEqual(self.review(), ["- Nothing proposed or decided this week."])

    def test_an_edit_made_before_the_flag_existed_still_shows(self):
        self.settle("next_action", 1, teacher_history=["it is a waiting item"])
        self.assertEqual(self.review(), [*HEAD, row("Next action", 1, 0, 1, 0, 0), NONE_YET])


class OrderTests(TrustCase):
    def test_rows_follow_the_names_you_see_not_the_codes(self):
        self.settle("follow_up", 1)  # Follow-up
        self.settle("document", 1)  # Inbox file
        self.assertEqual(self.review(), [*HEAD, row("Follow-up", 1, 1, 0, 0, 0), row("Inbox file", 1, 1, 0, 0, 0),
                                         NONE_YET])


class AutoTests(TrustCase):
    def extra_config(self):
        return '[trust]\nbookkeeping = ["next_action"]'

    def test_a_trusted_kind_counts_as_auto_not_approved(self):
        self.capture("Water the plants #anywhere", "Call the dentist #calls")
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.review(), [*HEAD, row("Next action", 2, 0, 0, 0, 2), NONE_YET])


class UndoTests(TrustCase):
    def undo_last(self, day: str = "2026-10-01") -> None:
        with self.clock(day):
            self.agent.undo(self.agent.ledger.list_undo()[0]["id"])

    def test_an_approval_you_took_back_counts_as_rejected(self):
        plants, dentist = self.capture("Water the plants #anywhere", "Call the dentist #calls")
        self.decide("approve", plants)
        self.decide("approve", dentist)
        self.undo_last()
        self.assertEqual(self.review(), [*HEAD, row("Next action", 2, 1, 0, 1, 0), NONE_YET])


class PrivateTests(TrustCase):
    def test_the_report_names_no_item(self):
        (secret,) = self.capture("Call the clinic about my results #anywhere #private")
        self.decide("approve", secret)
        lines = self.review()
        self.assertEqual(lines, [*HEAD, row("Next action", 1, 1, 0, 0, 0), NONE_YET])
        self.assertNotIn("clinic", "\n".join(lines))


class EditedTests(TrustCase):
    """Approved after you changed it: ticks moved, another start day picked, or a correction sent to the model."""

    def extra_files(self):
        return {CLUB: "---\nkey: AREA_CLUB\ntype: area\naliases: [Club]\nprivate: false\n---\n# AREA_CLUB\n\n"
                      "## Recurring\n"}

    def decision(self) -> str:
        """A decision with ticked and unticked items, as the agent proposes it."""
        hub = self.read(LAB).replace("## Log", "## Decisions\nFull list: [[DEMO_PROJECT_DECISIONS]].\n\n## Log", 1)
        self.write(LAB, hub)
        self.write("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT_DECISIONS.md", DECISIONS)
        self.provider.decided_answers.append({
            "decision": "We book room 101", "why": "", "question_ids": ["q2"], "section": "Budget", "steps": [],
            "explanation": ""})
        return self.capture("Decided: We book room 101 [[DEMO_PROJECT]]")[0]

    def answer(self, label: str, proposal_id: str, day: str = "2026-09-30") -> None:
        with self.clock(day):
            self.tick(label, proposal_id)
            self.agent.sync_review_requests()

    def duty(self) -> str:
        found = self.provider.interpretations
        found.append(draft(title="Prepare the DEMO_CLUB agenda by 1:30pm", recurrence="every week on Monday",
                           area_key="AREA_CLUB"))
        return self.capture("DEMO_CLUB agenda every Monday by 1:30pm for the club")[0]

    def test_the_ticks_you_leave_as_proposed_are_an_approval(self):
        self.answer("Approve", self.decision())
        self.assertEqual(self.review(), [*HEAD, row("Decision", 1, 1, 0, 0, 0), NONE_YET])

    def test_moving_a_tick_is_an_edit(self):
        proposal_id = self.decision()
        with self.clock("2026-09-30"):
            text = self.read("_agent/APPROVAL.md")
            self.write("_agent/APPROVAL.md", text.replace("- [ ] 1. Close question", "- [x] 1. Close question", 1))
        self.answer("Approve", proposal_id)
        self.assertEqual(self.review(), [*HEAD, row("Decision", 1, 0, 1, 0, 0), NONE_YET])

    def test_a_start_day_other_than_the_one_shown_is_an_edit(self):
        self.answer("Approve · show on the due day", self.duty())
        self.assertEqual(self.review(), [*HEAD, row("Next action", 1, 0, 1, 0, 0), NONE_YET])

    def test_the_start_day_shown_is_an_approval(self):
        self.answer("Approve · show 2 days before (as shown)", self.duty())
        self.assertEqual(self.review(), [*HEAD, row("Next action", 1, 1, 0, 0, 0), NONE_YET])

    def correct(self, proposal_id: str, words: str, revised: dict) -> str:
        """Send a correction to the model; the ID of the revised proposal."""
        self.provider.interpretations.append(revised)
        with self.clock("2026-09-30"):
            text = self.read("_agent/APPROVAL.md")
            marker = f"<!-- gtd-agent:proposal id={proposal_id} -->"
            start = text.index(marker) + len(marker)
            end = text.index(ITEM_END, start)
            body = with_feedback(text[start:end], words).replace("- [ ] Teacher feedback", "- [x] Teacher feedback")
            self.write("_agent/APPROVAL.md", text[:start] + "\n" + body + "\n" + text[end:])
            self.agent.sync_review_requests()
        found = self.pending("inbox")[0]["id"]
        self.assertNotEqual(found, proposal_id)
        return found

    def test_a_correction_sent_to_the_model_is_an_edit(self):
        self.provider.interpretations.append(draft(title="Ask Jordan for the room"))
        proposal_id = self.capture("Jordan will send the room confirmation")[0]
        revised = self.correct(proposal_id, "Jordan owes me this; it is a waiting item.",
                               draft(route="waiting_for", person="Jordan", what="Room confirmation"))
        self.answer("Approve", revised)
        self.assertEqual(self.review(), [*HEAD, row("Next action", 1, 0, 1, 0, 0), NONE_YET])  # the first kind

    def test_a_second_correction_still_charges_the_kind_first_proposed(self):
        self.provider.interpretations.append(draft(title="Ask Jordan for the room"))
        proposal_id = self.capture("Jordan will send the room confirmation")[0]
        once = self.correct(proposal_id, "Jordan owes me this; it is a waiting item.",
                            draft(route="waiting_for", person="Jordan", what="Room confirmation"))
        twice = self.correct(once, "Say it is due Friday.",
                             draft(route="waiting_for", person="Jordan", what="Room confirmation by Friday"))
        self.answer("Approve", twice)
        self.assertEqual(self.review(), [*HEAD, row("Next action", 1, 0, 1, 0, 0), NONE_YET])


class SuggestionTests(TrustCase):
    """A kind that earned it is suggested for [trust] bookkeeping. Rules over the ledger; nothing is changed."""

    def test_eight_approvals_as_written_on_three_days_earn_a_suggestion(self):
        self.spread("next_action", 8)
        self.assertEqual(self.review(), [*HEAD, row("Next action", 8, 8, 0, 0, 0),
                                         self.suggestion("Next action", "next_action", 8), HOW_TO])

    def test_seven_are_not_enough(self):
        self.spread("next_action", 7)
        self.assertEqual(self.review(), [*HEAD, row("Next action", 7, 7, 0, 0, 0), NONE_YET])

    def test_one_sitting_is_not_a_track_record(self):
        self.settle("next_action", 12)
        self.assertEqual(self.review(), [*HEAD, row("Next action", 12, 12, 0, 0, 0), NONE_YET])

    def test_two_days_are_not_enough(self):
        self.spread("next_action", 12, days=DAYS[:2])
        self.assertEqual(self.review(), [*HEAD, row("Next action", 12, 12, 0, 0, 0), NONE_YET])

    def test_a_day_it_applied_on_its_own_is_not_a_day_you_approved(self):
        self.spread("next_action", 8, days=DAYS[:2])
        self.settle("next_action", 1, day=DAYS[2], trusted=True)  # trusted once, since taken off the list
        self.assertEqual(self.review(), [*HEAD, row("Next action", 9, 8, 0, 0, 1), NONE_YET])

    def test_the_days_are_those_you_approved_not_those_it_was_proposed(self):
        ids = self.settle("next_action", 8, status="pending", day="2026-09-28")
        for n, proposal_id in enumerate(ids):
            with self.clock(DAYS[n % 3]):
                self.agent.ledger.set_status(proposal_id, "committed")
        self.assertEqual(self.review(), [*HEAD, row("Next action", 8, 8, 0, 0, 0),
                                         self.suggestion("Next action", "next_action", 8), HOW_TO])

    def test_the_days_are_counted_in_the_suggestion(self):
        self.spread("next_action", 10, days=(*DAYS, "2026-09-30"))
        self.assertEqual(self.review()[-2:], [self.suggestion("Next action", "next_action", 10, 4), HOW_TO])

    def test_the_count_covers_four_weeks_not_just_this_one(self):
        self.settle("next_action", 3, day="2026-09-07")  # the first day of the 28
        self.settle("next_action", 3, day="2026-09-20")
        self.settle("next_action", 2, day="2026-10-04")  # the last
        self.assertEqual(self.review(), [*HEAD, row("Next action", 2, 2, 0, 0, 0),
                                         self.suggestion("Next action", "next_action", 8), HOW_TO])

    def test_an_approval_older_than_four_weeks_does_not_count(self):
        self.spread("next_action", 7)
        self.settle("next_action", 1, day="2026-09-06")
        self.assertEqual(self.review(), [*HEAD, row("Next action", 7, 7, 0, 0, 0), NONE_YET])

    def test_a_quiet_week_still_shows_what_the_month_earned(self):
        self.spread("waiting_for", 8, days=("2026-09-15", "2026-09-16", "2026-09-17"))
        self.assertEqual(self.review(), ["- Nothing proposed or decided this week.",
                                         self.suggestion("Waiting for", "waiting_for", 8), HOW_TO])

    def test_one_no_keeps_a_kind_off_the_list(self):
        self.spread("next_action", 8)
        self.settle("next_action", 1, status="rejected")
        self.assertEqual(self.review(), [*HEAD, row("Next action", 9, 8, 0, 1, 0), NONE_YET])

    def test_one_approval_taken_back_keeps_a_kind_off_the_list(self):
        ids = self.spread("next_action", 8)
        self.take_back(ids[:1])
        self.assertEqual(self.review(), [*HEAD, row("Next action", 8, 7, 0, 1, 0), NONE_YET])

    def test_one_edit_keeps_a_kind_off_the_list(self):
        self.spread("next_action", 8)
        self.settle("next_action", 1, edited=True)
        self.assertEqual(self.review(), [*HEAD, row("Next action", 9, 8, 1, 0, 0), NONE_YET])

    def test_each_kind_is_judged_on_its_own(self):
        self.spread("next_action", 8)
        self.spread("waiting_for", 8)
        self.settle("waiting_for", 1, status="rejected")
        self.assertEqual(self.review(), [*HEAD, row("Next action", 8, 8, 0, 0, 0), row("Waiting for", 9, 8, 0, 1, 0),
                                         self.suggestion("Next action", "next_action", 8), HOW_TO])

    def test_two_kinds_are_suggested_in_alphabetical_order(self):
        self.spread("waiting_for", 9)
        self.spread("next_action", 8)
        lines = self.review()
        self.assertEqual(lines[-3:], [self.suggestion("Next action", "next_action", 8),
                                      self.suggestion("Waiting for", "waiting_for", 9), HOW_TO])

    def test_a_kind_that_cannot_be_trusted_is_never_suggested(self):
        self.spread("contact", 20)
        self.assertEqual(self.review(), [*HEAD, row("Contact", 20, 20, 0, 0, 0), NONE_YET])

    def test_questions_are_not_approvals(self):
        for kind in ("manual", "drop_reason", "after_event", "lint_choice"):
            self.spread(kind, 10)
        self.assertEqual(self.review(), ["- Nothing proposed or decided this week."])


class AlreadyTrustedTests(TrustCase):
    def extra_config(self):
        return '[trust]\nbookkeeping = ["next_action"]'

    def test_a_kind_already_on_is_not_suggested(self):
        self.settle("next_action", 12, trusted=True)
        self.assertEqual(self.review(), [*HEAD, row("Next action", 12, 0, 0, 0, 12), NONE_YET])

    def test_undoing_what_it_applied_says_to_take_it_back(self):
        ids = self.settle("next_action", 9, trusted=True)
        self.take_back(ids[:2])
        self.assertEqual(self.review(), [*HEAD, row("Next action", 9, 0, 0, 2, 7),
                                         "- Take back auto-apply: Next action (`next_action`) · you undid 2 of 9 in 28 "
                                         "days. Remove it from `bookkeeping` in config.toml, then run `run.ps1 restart`.",
                                         NONE_YET])

    def test_a_kind_you_approved_before_turning_it_on_is_not_suggested_again(self):
        self.spread("next_action", 9)
        self.assertEqual(self.review(), [*HEAD, row("Next action", 9, 9, 0, 0, 0), NONE_YET])

    def test_undoing_something_you_approved_yourself_is_not_a_reason_to_take_it_back(self):
        ids = self.settle("next_action", 9)
        self.take_back(ids[:2])
        self.assertEqual(self.review(), [*HEAD, row("Next action", 9, 7, 0, 2, 0), NONE_YET])

    def test_one_no_on_an_unapplied_proposal_is_not_a_reason_to_take_it_back(self):
        self.settle("next_action", 3, trusted=True)
        self.settle("next_action", 1, status="rejected")
        self.assertEqual(self.review(), [*HEAD, row("Next action", 4, 0, 0, 1, 3), NONE_YET])


class CommandTests(TrustCase):
    """run.ps1 trust prints the report for the current week."""

    def command(self) -> tuple[int, list[str]]:
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(Agent, "today", lambda self: date(2026, 9, 30)):  # a Wednesday
            code = main(["--config", str(self.settings.config_path), "trust"])
        return code, out.getvalue().splitlines()

    def test_trust_prints_what_the_weekly_review_shows(self):
        (monday,) = self.capture("Water the plants #anywhere", day="2026-09-28")
        (sunday,) = self.capture("Call the dentist #calls", day="2026-10-04")
        self.decide("approve", monday, "2026-10-04")
        self.decide("reject", sunday, "2026-10-04")
        self.assertEqual(self.command(), (0, [*HEAD, row("Next action", 2, 1, 0, 1, 0), NONE_YET]))

    def test_trust_with_nothing_to_count_says_so(self):
        self.assertEqual(self.command(), (0, ["- Nothing proposed or decided this week."]))


if __name__ == "__main__":
    unittest.main()
