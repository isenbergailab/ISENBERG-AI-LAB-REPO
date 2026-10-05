import io
import json
import unittest
from contextlib import redirect_stdout

from helpers import TODAY, VaultCase, draft
from gtd_agent.cli import main
from gtd_agent.core import parse_captures, read_snapshot
from gtd_agent.feedback import with_feedback
from gtd_agent.workflow import ITEM_END, PROPOSAL_VERSION


class ExplicitCaptureTests(VaultCase):
    """Vault syntax is filed without any model, even with remote inference off."""

    def test_context_tag_becomes_single_action_and_line_clears(self):
        self.add_inbox("Buy printer ink #errands 📅 2026-10-02")
        result = self.agent.scan()
        proposal = result.new_proposals[0]["proposal"]
        self.assertEqual(proposal["kind"], "next_action")
        self.assertTrue(proposal["explicit"])
        self.tick()
        self.assertTrue(self.agent.sync_review_requests()[0].startswith("Applied next_action"))
        singles = self.read("01_GTD/SINGLE_ACTIONS.md")
        self.assertRegex(singles, r"## @errands\n- \[ \] Buy printer ink #errands 📅 2026-10-02 ➕ 2026-09-28 \^gtd-")
        self.assertEqual(parse_captures(read_snapshot(self.path("00_INBOX/INBOX.md")), 6000), [])
        self.assertIn("Approved · Next action: Buy printer ink", self.read("_agent/LOG.md"))

    def test_project_link_queues_a_step_behind_the_current_next(self):
        self.add_inbox("Draft the charter [[DEMO_PROJECT]] #computer #30m")
        self.agent.scan()
        self.tick(suffix=" ✅ 2026-09-28")  # the Tasks plugin appends a done date when a box is clicked
        self.agent.sync_review_requests()
        text = self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md")
        lines = text.split("\n")
        added = next(i for i, line in enumerate(lines) if "Draft the charter" in line)
        self.assertEqual(lines[added - 1], "- [ ] Plan first meeting #computer")  # end of the open steps
        self.assertEqual(lines[added + 2], "### Done")
        self.assertIn("Draft the charter #computer #30m", lines[added])
        self.assertNotIn("#next", lines[added])

    def test_explicit_next_goes_straight_to_next_actions(self):
        self.add_inbox("Draft the charter [[DEMO_PROJECT]] #computer #next")
        self.agent.scan()
        self.tick()
        self.agent.sync_review_requests()
        lines = self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md").split("\n")
        added = next(i for i, line in enumerate(lines) if "Draft the charter" in line)
        self.assertEqual(lines[added - 1], "- [ ] Email the advisor #computer #next")
        self.assertIn("Draft the charter #computer #next", lines[added])

    def test_repeat_rule_files_an_area_duty(self):
        self.add_inbox("Write board agenda (by 1:30pm) [[AREA_CLUB]] #computer 🔁 every week on monday")
        proposal = self.agent.scan().new_proposals[0]["proposal"]
        self.assertEqual(proposal["summary"], "Recurring duty in AREA_CLUB › Recurring")
        self.assertTrue(proposal["ops"][0]["line"].startswith(
            "- [ ] Write board agenda (by 1:30pm) #computer #next 🔁 every week on Monday 🛫 2026-09-26 📅 2026-09-28 "
            "➕ 2026-09-28"))
        self.tick()
        self.agent.sync_review_requests()
        self.assertIn("## Recurring\n- [ ] Write board agenda", self.read("03_AREAS/AREA_CLUB.md"))

    def test_bad_repeat_rule_is_explained(self):
        self.add_inbox("Water plants #anywhere 🔁 sometimes")
        proposal = self.agent.scan().new_proposals[0]["proposal"]
        self.assertEqual(proposal["kind"], "manual")
        self.assertIn("every week on Monday", proposal["reason"])

    def test_waiting_syntax_defaults_follow_up(self):
        self.add_inbox("- [ ] #waiting Morgan Lee: budget numbers [[DEMO_PROJECT]]")
        proposal = self.agent.scan().new_proposals[0]["proposal"]
        line = proposal["ops"][0]["line"]
        self.assertTrue(line.startswith("- [ ] #waiting Morgan Lee: budget numbers 📅 2026-10-05 ➕ 2026-09-28"))
        self.assertEqual(proposal["ops"][0]["section"], "Waiting For")

    def test_private_or_unknown_capture_stays_manual(self):
        self.add_inbox("call the bank about fees #private", "Plan [[NOPE]] #computer", "think about summer")
        kinds = [p["proposal"] for p in self.agent.scan().new_proposals]
        self.assertEqual([p["kind"] for p in kinds], ["manual", "manual", "manual"])
        self.assertIn("Private capture", kinds[0]["reason"])
        self.assertIn("Unknown link: NOPE", kinds[1]["reason"])
        self.assertIn("Remote inference is off", kinds[2]["reason"])
        self.assertEqual(self.provider.calls, [])
        review = self.read("_agent/APPROVAL.md")
        block = review.split("call the bank", 1)[1].split(ITEM_END, 1)[0]
        self.assertNotIn("Teacher feedback", block)  # private text never goes to a model

    def test_secret_is_redacted(self):
        self.add_inbox("api_key = " + "sk" + "-" + "a" * 22 + " #computer")
        proposal = self.agent.scan().new_proposals[0]["proposal"]
        self.assertEqual(proposal["source"], "[redacted]")
        self.assertNotIn("sk-abcdef", self.read("_agent/APPROVAL.md"))

    def test_reject_keeps_the_line_and_is_not_reproposed(self):
        self.add_inbox("Buy stamps #errands")
        self.agent.scan()
        self.tick("Reject")
        self.agent.sync_review_requests()
        self.assertIn("Buy stamps", self.read("00_INBOX/INBOX.md"))
        self.assertEqual(self.agent.scan().new_proposals, [])

    def test_edited_preview_is_refused(self):
        self.add_inbox("Buy stamps #errands")
        self.agent.scan()
        text = self.read("_agent/APPROVAL.md").replace("Buy stamps #errands ➕", "Buy MANY stamps #errands ➕")
        self.write("_agent/APPROVAL.md", text.replace("- [ ] Approve", "- [x] Approve", 1))
        self.assertIn("preview text was edited", self.agent.sync_review_requests()[0])
        self.assertNotIn("stamps", self.read("01_GTD/SINGLE_ACTIONS.md"))

    def test_identical_lines_need_distinct_wording(self):
        self.add_inbox("Buy stamps #errands", "Buy stamps #errands")
        kinds = {p["proposal"]["kind"] for p in self.agent.scan().new_proposals}
        self.assertEqual(kinds, {"manual"})

    def test_undo_restores_the_note(self):
        before = self.read("01_GTD/SINGLE_ACTIONS.md")
        self.add_inbox("Buy stamps #errands")
        self.agent.scan()
        self.tick()
        self.agent.sync_review_requests()
        self.tick("Undo")
        self.assertIn("Reversed", self.agent.sync_review_requests()[0])
        self.assertEqual(self.read("01_GTD/SINGLE_ACTIONS.md"), before)


class ModelCaptureTests(VaultCase):
    remote = True

    def scan_one(self, text, value):
        self.add_inbox(text)
        self.provider.interpretations.append(value)
        return self.agent.scan().new_proposals[0]

    def test_next_action_for_a_project(self):
        item = self.scan_one("need to email the advisor again about the lab",
                             draft(title="Email the advisor again", project_key="DEMO_PROJECT", time_estimate="15m"))
        self.assertEqual(item["proposal"]["summary"], "Step queued in DEMO_PROJECT › Steps (gets #next when its turn comes)")
        self.assertIn("Email the advisor again #computer #15m", item["proposal"]["ops"][0]["line"])
        self.assertEqual(item["proposal"]["ops"][0]["position"], "queue")
        call = dict(self.provider.calls)["interpret"]
        self.assertIn("DEMO_PROJECT", [p["key"] for p in call["projects"]])
        self.assertNotIn("TAXES", [p["key"] for p in call["projects"]])  # private area project is never sent
        self.assertTrue(all("Riley" not in i["title"] for i in call["items"]))

    def test_waiting_dates_are_resolved_by_python(self):
        item = self.scan_one("waiting for Jordan to answer my 9/23 email, should hear by 10/1",
                             draft(route="waiting_for", person="Jordan", what="Answer to my email",
                                   since_evidence="9/23", due_evidence="10/1"))
        line = item["proposal"]["ops"][0]["line"]
        self.assertIn("#waiting Jordan: Answer to my email 📅 2026-10-01 ➕ 2026-09-23", line)
        self.assertIn("Missing year uses 2026", item["proposal"]["clarification"]["assumptions"])

    def test_start_date_is_not_a_deadline(self):
        item = self.scan_one("start studying for FAR on October 13th",
                             draft(title="Start studying for FAR", start_evidence="October 13th", context="#computer"))
        line = item["proposal"]["ops"][0]["line"]
        self.assertIn("🛫 2026-10-13", line)
        self.assertNotIn("📅", line)

    def test_invented_evidence_becomes_manual(self):
        item = self.scan_one("email Sam", draft(route="waiting_for", person="Jordan", what="Reply"))
        self.assertEqual(item["proposal"]["kind"], "manual")
        self.assertIn("copied from the capture", item["proposal"]["reason"])

    def test_completion_ticks_the_matched_task(self):
        self.add_inbox("I emailed the advisor today")
        self.provider.interpretations.append(lambda text, projects, areas, items: draft(
            route="completion_report", action_id=next(i["id"] for i in items if "advisor" in i["title"]),
            completion_evidence="I emailed the advisor"))
        self.agent.scan()
        self.tick()
        self.agent.sync_review_requests()
        self.assertIn("- [x] Email the advisor #computer #next ✅ 2026-09-28",
                      self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"))

    def test_new_project_creates_one_note(self):
        self.scan_one("new project: Biology 101 notes, first read chapter 1",
                      draft(route="new_project", name_excerpt="Biology 101", title="Organize Biology 101 notes",
                            first_step="read chapter 1", area_key="AREA_CLUB", context="#anywhere"))
        self.tick()
        self.agent.sync_review_requests()
        note = self.read("02_PROJECTS/BIOLOGY_101/BIOLOGY_101.md")
        self.assertIn("status: active", note)
        self.assertIn('area: "[[AREA_CLUB]]"', note)
        self.assertIn("- [ ] read chapter 1 #anywhere #next ➕ 2026-09-28", note)
        self.tick("Undo")
        self.agent.sync_review_requests()
        self.assertFalse(self.path("02_PROJECTS/BIOLOGY_101").exists())

    def test_project_note_goes_to_the_log(self):
        self.scan_one("the lab got 12 signups", draft(route="project_note", project_key="DEMO_PROJECT",
                                                      title="Lab got 12 signups"))
        self.tick()
        self.agent.sync_review_requests()
        self.assertIn("## Log\n- 2026-09-28 Lab got 12 signups\n- 2026-09-20", self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"))

    def test_someday_project_gets_a_plain_step(self):
        item = self.scan_one("buy the latin book", draft(project_key="LATIN", title="Buy the Latin textbook"))
        self.assertEqual(item["proposal"]["summary"], "Step in LATIN › Steps (project is someday, so no #next)")
        self.assertNotIn("#next", item["proposal"]["ops"][0]["line"])

    def test_repeat_from_free_text(self):
        item = self.scan_one("agenda for the club board every monday",
                             draft(title="Write board agenda", area_key="AREA_CLUB", recurrence="every week on monday"))
        self.assertEqual(item["proposal"]["summary"], "Recurring duty in AREA_CLUB › Recurring")
        self.assertIn("🔁 every week on Monday 🛫 2026-09-26 📅 2026-09-28", item["proposal"]["ops"][0]["line"])
        invented = self.scan_one("write the club agenda", draft(title="Write agenda", recurrence="every day"))
        self.assertEqual(invented["proposal"]["kind"], "manual")

    def test_someday_project_review_date(self):
        item = self.scan_one("someday build an llm from scratch, in a year",
                             draft(route="new_project", name_excerpt="llm from scratch", project_status="someday",
                                   title="Build an LLM from scratch", start_evidence="in a year"))
        text = item["proposal"]["ops"][0]["text"]
        self.assertIn("status: someday", text)
        self.assertIn("review: 2027-09-28", text)
        vague = self.scan_one("after my cpa exams, learn rust",
                              draft(route="new_project", name_excerpt="learn rust", project_status="someday",
                                    title="Learn Rust", start_evidence="after my cpa exams"))
        text = vague["proposal"]["ops"][0]["text"]
        self.assertIn("review:\n", text)
        self.assertIn("is not a date", text)

    def test_research_request_needs_a_project(self):
        item = self.scan_one("build an llm from scratch with raschka's book, in a year #research",
                             draft(route="new_project", name_excerpt="llm from scratch", project_status="someday",
                                   title="Build an LLM from scratch", start_evidence="in a year"))
        ops = item["proposal"]["ops"]
        self.assertEqual([op["path"] for op in ops], ["02_PROJECTS/LLM_FROM_SCRATCH/LLM_FROM_SCRATCH.md",
                                                      "_agent/RESEARCH_REQUESTS/LLM_FROM_SCRATCH.md"])
        self.assertIn("request: LLM_FROM_SCRATCH", ops[1]["text"])
        line = self.scan_one("sailing ideas #research", draft(route="someday_maybe", title="Sailing"))
        self.assertEqual(line["proposal"]["kind"], "manual")

    def test_teacher_feedback_revises_then_approval_teaches(self):
        self.scan_one("Jordan will send the room confirmation", draft(title="Ask Jordan for the room"))
        row = self.pending("inbox")[0]
        self.provider.interpretations.append(draft(route="waiting_for", person="Jordan", what="Room confirmation",
                                                   lesson="Promised replies are Waiting For."))
        text = self.read("_agent/APPROVAL.md")
        marker = f"<!-- gtd-agent:proposal id={row['id']} -->"
        start = text.index(marker) + len(marker)
        end = text.index(ITEM_END, start)
        body = with_feedback(text[start:end], "Jordan owes me this; it is a waiting item.")
        body = body.replace("- [ ] Teacher feedback", "- [x] Teacher feedback")
        self.write("_agent/APPROVAL.md", text[:start] + "\n" + body + "\n" + text[end:])
        outcome = self.agent.sync_review_requests()[0]
        self.assertIn("revised proposal", outcome)
        revised = self.pending("inbox")[0]
        self.assertNotEqual(revised["id"], row["id"])
        self.assertEqual(json.loads(revised["proposal_json"])["kind"], "waiting_for")
        self.assertEqual(self.provider.calls[-1][1]["feedback"], ["Jordan owes me this; it is a waiting item."])
        self.tick()
        self.agent.sync_review_requests()
        lesson = json.loads(self.agent.ledger.list_learning()[0]["example_json"])
        self.assertEqual(lesson["route"], "waiting_for")
        self.assertIn("Jordan owes me", lesson["teacher_feedback"][0])

    def test_split_capture_clears_only_after_every_part(self):
        self.add_inbox("email Sam the budget and buy printer ink")
        self.provider.decisions.append({"operation": ("new_capture", 0.9), "category": ("next_action", 0.6),
                                        "multiplicity": ("multiple", 0.8)})
        self.provider.splits.append(["email Sam the budget", "buy printer ink"])
        self.provider.interpretations += [draft(title="Email Sam the budget"),
                                          draft(title="Buy printer ink", context="#errands")]
        items = self.agent.scan().new_proposals
        self.assertEqual(len(items), 2)
        self.assertEqual(items[1]["proposal"]["item"], {"index": 2, "count": 2})
        first, second = self.pending("inbox")
        self.tick(proposal_id=first["id"])
        self.agent.sync_review_requests()
        self.assertIn("buy printer ink", self.read("00_INBOX/INBOX.md"))
        self.tick(proposal_id=second["id"])
        self.agent.sync_review_requests()
        self.assertNotIn("printer", self.read("00_INBOX/INBOX.md"))
        self.assertEqual(self.agent.scan().new_proposals, [])

    def test_ungrounded_split_falls_back_to_one_item(self):
        self.add_inbox("email Sam the budget")
        self.provider.decisions.append({"operation": ("new_capture", 0.9), "category": ("next_action", 0.6),
                                        "multiplicity": ("multiple", 0.8)})
        self.provider.splits.append(["email Sam the budget", "call the governor tomorrow"])
        self.provider.interpretations.append(draft(title="Email Sam the budget"))
        self.assertEqual(len(self.agent.scan().new_proposals), 1)

    def test_cached_result_is_not_requested_twice(self):
        self.scan_one("email Sam", draft(title="Email Sam"))
        self.agent.scan()
        self.assertEqual([c[0] for c in self.provider.calls], ["decide", "interpret"])

    def test_stale_proposal_rebuilds_without_a_paid_call(self):
        self.add_inbox("I emailed the advisor today")
        self.provider.interpretations.append(lambda text, projects, areas, items: draft(
            route="completion_report", action_id=next(i["id"] for i in items if "advisor" in i["title"]),
            completion_evidence="I emailed the advisor"))
        self.agent.scan()
        # You tick the task yourself before approving: the reviewed line is gone.
        path = "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"
        self.write(path, self.read(path).replace("- [ ] Email the advisor", "- [x] Email the advisor"))
        self.tick()
        self.assertIn("Not applied", self.agent.sync_review_requests()[0])
        calls = len(self.provider.calls)
        rebuilt = self.agent.scan().new_proposals[0]["proposal"]
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual(rebuilt["kind"], "manual")
        self.assertEqual(rebuilt["engine_version"], PROPOSAL_VERSION)


class TypingTests(VaultCase):
    """The Inbox is read once it is quiet (untouched inbox_quiet_seconds), so no line is captured half-typed."""
    remote = True
    inbox_quiet = 30

    def test_a_line_you_are_still_typing_waits_until_the_inbox_is_quiet(self):
        self.add_inbox("need to email the advisor ag")  # saved a moment ago, mid-word
        result = self.agent.scan()
        self.assertEqual(result.new_proposals, [])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.pending(), [])
        self.assertTrue(0 < result.quiet_in <= 30, result.quiet_in)
        inbox = self.read("00_INBOX/INBOX.md")
        self.write("00_INBOX/INBOX.md", inbox.replace("advisor ag", "advisor again about the lab"))
        self.inbox_untouched(31)
        self.provider.interpretations.append(draft(title="Email the advisor again", project_key="DEMO_PROJECT"))
        result = self.agent.scan()
        self.assertIsNone(result.quiet_in)
        self.assertEqual([item["proposal"]["source"] for item in result.new_proposals],
                         ["need to email the advisor again about the lab"])
        self.assertEqual(len(self.pending()), 1)

    def test_a_file_time_ahead_of_the_clock_never_holds_the_inbox_back(self):
        self.add_inbox("Buy stamps #errands")
        self.inbox_untouched(-3600)  # a synced edit, stamped by a device whose clock runs an hour ahead
        self.assertEqual(len(self.agent.scan().new_proposals), 1)

    def test_a_line_the_agent_adds_is_read_at_once(self):
        self.inbox_untouched(600)
        self.agent.append_to_inbox(["Buy stamps #errands"])  # mail to yourself, Telegram
        self.assertEqual(len(self.agent.scan().new_proposals), 1)

    def test_a_line_the_agent_adds_does_not_end_the_wait_for_yours(self):
        self.add_inbox("need to email the advisor ag")
        self.agent.append_to_inbox(["Buy stamps #errands"])
        result = self.agent.scan()
        self.assertEqual(result.new_proposals, [])
        self.assertTrue(0 < result.quiet_in <= 30, result.quiet_in)

    def test_the_agent_removing_an_approved_line_is_not_typing(self):
        self.add_inbox("Buy stamps #errands", "Call Sam about the keys #calls")
        self.inbox_untouched(600)
        self.assertEqual(len(self.agent.scan().new_proposals), 2)
        self.tick()
        self.assertTrue(self.agent.sync_review_requests()[0].startswith("Applied"))
        self.assertNotIn("Buy stamps", self.read("00_INBOX/INBOX.md"))
        self.assertIsNone(self.agent.scan().quiet_in)


class InboxCommandTests(VaultCase):
    inbox_quiet = 30

    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--config", str(self.settings.config_path), *args])
        return code, out.getvalue()

    def test_the_scan_command_reads_the_inbox_now(self):
        self.add_inbox("Buy stamps #errands")
        code, said = self.cli("scan")
        self.assertEqual(code, 0)
        self.assertIn("1 proposals, 0 already handled", said)

    def test_the_capture_command_adds_one_inbox_line(self):
        code, said = self.cli("capture", "Buy", "stamps", "#errands")
        self.assertEqual((code, said), (0, "Added to the Inbox\n"))
        self.assertTrue(self.read("00_INBOX/INBOX.md").endswith("%%\n- Buy stamps #errands\n"))


if __name__ == "__main__":
    unittest.main()
