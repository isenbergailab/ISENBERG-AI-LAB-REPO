import json
import re
import unittest
from datetime import datetime

from helpers import VaultCase
from gtd_agent.housekeeping import AUTO_BEGIN, AUTO_END, Housekeeper

PROJECT = "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"


class PromotionTests(VaultCase):
    def test_ticking_the_last_next_step_promotes_the_first_open_step(self):
        keeper = Housekeeper(self.agent)
        keeper.run()  # first sight only records state
        self.write(PROJECT, self.read(PROJECT).replace("- [ ] Email the advisor #computer #next",
                                                       "- [x] Email the advisor #computer #next ✅ 2026-09-28"))
        messages = keeper.run()
        self.assertIn("DEMO_PROJECT: next step promoted", messages)
        self.assertIn("- [ ] Book the room #next #calls", self.read(PROJECT))
        self.assertIn("Auto · DEMO_PROJECT", self.read("_agent/LOG.md"))
        self.assertEqual(self.agent.ledger.list_undo()[0]["summary"], "Promoted next step in DEMO_PROJECT: Book the room")

    def test_deleting_the_next_step_does_not_promote(self):
        keeper = Housekeeper(self.agent)
        keeper.run()
        self.write(PROJECT, self.read(PROJECT).replace("- [ ] Email the advisor #computer #next\n", ""))
        keeper.run()
        self.assertNotIn("Book the room #next", self.read(PROJECT))


class PromotionApprovalTests(VaultCase):
    auto_promote = False

    def test_approval_mode_queues_a_proposal(self):
        keeper = Housekeeper(self.agent)
        keeper.run()
        self.write(PROJECT, self.read(PROJECT).replace("- [ ] Email the advisor #computer #next",
                                                       "- [x] Email the advisor #computer #next"))
        keeper.run()
        row = self.pending("promotion")[0]
        self.assertEqual(json.loads(row["proposal_json"])["title"], "Book the room")
        self.tick(proposal_id=row["id"])
        self.agent.sync_review_requests()
        self.assertIn("- [ ] Book the room #next #calls", self.read(PROJECT))


class FollowUpTests(VaultCase):
    def line(self, *words):
        return next(line for line in self.read(PROJECT).splitlines() if all(word in line for word in words))

    def follow_up_once(self, week: int = 0):
        """Approve the admin office follow-up, tick it, let the agent move the wait, then let a week pass (the wait
        comes due again on a date of its own, as it would after each 7-day move)."""
        Housekeeper(self.agent).run()
        row = next(r for r in self.pending("followup") if "Admin office" in r["proposal_json"])
        self.tick(proposal_id=row["id"])
        self.agent.sync_review_requests()
        step = self.line("- [ ] Follow up with Admin office")
        self.write(PROJECT, self.read(PROJECT).replace(step, step.replace("- [ ]", "- [x]", 1)))
        Housekeeper(self.agent).run()
        wait = self.line("#waiting Admin office")
        self.write(PROJECT, self.read(PROJECT).replace(wait, re.sub(r"📅 \S+", f"📅 2026-09-{25 - week}", wait)))

    def test_an_overdue_wait_proposes_a_follow_up_that_moves_the_wait_once_ticked(self):
        Housekeeper(self.agent).run()
        rows = {json.loads(r["proposal_json"])["title"]: r for r in self.pending("followup")}
        self.assertEqual(set(rows), {"Follow up with Admin office re: room approval",
                                     "Follow up with Riley re: signed form"})
        self.tick(proposal_id=rows["Follow up with Admin office re: room approval"]["id"])
        self.agent.sync_review_requests()
        step = self.line("Follow up with Admin office")
        self.assertTrue(step.startswith("- [ ] Follow up with Admin office re: room approval #computer #next 📅 2026-09-28"))
        follow_id = step.split("🆔 ", 1)[1].split()[0]
        wait = self.line("#waiting Admin office")
        self.assertIn("📅 2026-09-26", wait)  # it moves when you follow up, not when you approve
        self.assertIn(f"⛔ {follow_id}", wait)
        Housekeeper(self.agent).run()
        titles = [json.loads(r["proposal_json"])["title"] for r in self.pending("followup")]
        self.assertEqual(titles, ["Follow up with Riley re: signed form"])
        self.write(PROJECT, self.read(PROJECT).replace(step, step.replace("- [ ]", "- [x]", 1)))
        Housekeeper(self.agent).run()
        wait = self.line("#waiting Admin office")
        self.assertIn("📅 2026-10-05", wait)
        self.assertNotIn("⛔", wait)
        self.assertIn("followed up with Admin office", self.read("_agent/LOG.md"))

    def test_the_third_unanswered_follow_up_flags_the_wait(self):
        for week in range(3):
            self.follow_up_once(week)
        Housekeeper(self.agent).run()
        self.assertFalse(any("Admin office" in r["proposal_json"] for r in self.pending("followup")))
        today = self.read("_agent/TODAY.md")
        self.assertIn("### No answer after 3 follow-ups (1)", today)
        flagged = today.split("### No answer after 3 follow-ups (1)", 1)[1].split("###", 1)[0]
        self.assertIn("Admin office: room approval", flagged)

    def test_single_action_follow_up_keeps_the_area_link(self):
        Housekeeper(self.agent).run()
        row = next(r for r in self.pending("followup") if "Riley" in r["proposal_json"])
        line = json.loads(row["proposal_json"])["ops"][0]["line"]
        self.assertTrue(line.startswith("- [ ] Follow up with Riley re: signed form [[AREA_ADMIN]] #computer"))

    def test_ticking_the_wait_withdraws_the_proposal(self):
        Housekeeper(self.agent).run()
        self.write(PROJECT, self.read(PROJECT).replace("- [ ] #waiting Admin office", "- [x] #waiting Admin office"))
        Housekeeper(self.agent).run()
        self.assertEqual(len(self.pending("followup")), 1)


class LintDigestTests(VaultCase):
    def extra_files(self):
        return {"01_GTD/SINGLE_ACTIONS.md": "# SINGLE_ACTIONS\n\n## @computer\n- [ ] Fix bike\n- [ ] Fix bike\n"
                                            "- [ ] Read [[The Demo]] notes #computer\n- [ ] See [[Missing Note]] #computer\n"
                                            "- [ ] Overdue thing #computer 📅 2026-09-20\n- [ ] Today thing #calls 📅 2026-09-28\n",
                "02_PROJECTS/stray.zip": "zip"}

    def test_lint_fixes_safe_problems_and_lists_what_is_left(self):
        Housekeeper(self.agent).run()
        report = self.read("_agent/LINT.md")
        self.assertIn("links `Missing Note`, which does not exist", report)
        self.assertIn("## Actions without a context tag (1)", report)  # one question for both copies
        self.assertIn("`02_PROJECTS/stray.zip`", report)
        self.assertNotIn("The Demo", report)
        self.assertNotIn("## Duplicate actions (1)", report)
        singles = self.read("01_GTD/SINGLE_ACTIONS.md")
        self.assertEqual(singles.count("Fix bike"), 1)
        self.assertIn("[[DEMO_PROJECT|The Demo]]", singles)
        titles = [json.loads(r["proposal_json"])["title"] for r in self.pending("lint")]
        self.assertEqual(titles, ["Pick a context · Fix bike"])

    def test_digest_uses_live_task_queries(self):
        Housekeeper(self.agent).run(now=datetime(2026, 9, 28, 9, 0))
        digest = self.read("_agent/TODAY.md")
        for heading in ("## Report", "## Needs you", "### Overdue and due today (2)", "### Follow-ups due (1)",
                        "## Next actions", "### @computer (5)"):
            self.assertIn(heading, digest)
        self.assertIn("```tasks\nnot done\ndue before tomorrow", digest)
        self.assertIn("Calendar off", digest)
        before = self.path("_agent/TODAY.md").stat().st_mtime_ns
        Housekeeper(self.agent).digest(self.agent.vault(), datetime(2026, 9, 28, 9, 5))
        self.assertEqual(self.path("_agent/TODAY.md").stat().st_mtime_ns, before)  # unchanged content: no write


class ParkedProjectTests(VaultCase):
    def extra_files(self):
        return {"02_PROJECTS/SAILING/SAILING.md": "---\nkey: SAILING\ntype: project\nstatus: someday\n"
                                                  "area: \"[[AREA_CLUB]]\"\n---\n# SAILING\n\n## Steps\n"
                                                  "- [ ] Find a class #computer #next\n- [ ] Buy gloves #errands\n"}

    def test_parking_a_project_takes_its_steps_off_next_actions(self):
        keeper = Housekeeper(self.agent)
        keeper.run()
        self.assertIn("- [ ] Find a class #computer\n", self.read("02_PROJECTS/SAILING/SAILING.md"))
        self.assertIn("Auto · Vault fix: Took #next off 1 step in SAILING (status someday)",
                      self.read("_agent/LOG.md"))
        keeper.run()  # removing the tag is not a completed step, so nothing is promoted back
        self.assertNotIn("#next", self.read("02_PROJECTS/SAILING/SAILING.md"))
        self.assertNotIn("SAILING", self.read("_agent/LINT.md"))


class TidyTests(VaultCase):
    def test_steps_ticked_before_today_move_under_done_in_any_order(self):
        keeper = Housekeeper(self.agent)
        keeper.run()
        text = self.read(PROJECT).replace("- [ ] Plan first meeting #computer", "- [x] Plan first meeting #computer ✅ 2026-09-27")
        text = text.replace("- [ ] Book the room #calls", "- [x] Book the room #calls ✅ 2026-09-28")
        self.write(PROJECT, text)
        messages = keeper.run()
        self.assertIn("Moved 1 finished step under Done in DEMO_PROJECT", messages)
        text = self.read(PROJECT)
        self.assertIn("## Steps\n- [ ] Email the advisor #computer #next\n- [x] Book the room #calls ✅ 2026-09-28\n", text)
        self.assertIn("### Done\n- [x] Plan first meeting #computer ✅ 2026-09-27\n- [x] Pick a name", text)
        self.assertNotIn("Book the room #next", text)  # a ticked step out of order is not a promotion
        self.assertIn("Auto · Moved 1 finished step", self.read("_agent/LOG.md"))
        self.assertEqual(self.agent.ledger.list_undo()[0]["summary"], "Moved 1 finished step under Done in DEMO_PROJECT")


class SomedayTests(VaultCase):
    remote = True

    def extra_files(self):
        return {"01_GTD/SOMEDAY_MAYBE.md": "# SOMEDAY_MAYBE\n\n## Ideas\n- Learn to juggle\n- Sail the Cape · review 2026-10-01\n"
                                          "- Sailing course in Boston\n\n## Added from the Inbox\n"
                                          "- Buy a used dinghy · review 2027-05-01\n- Private thing #private\n"}

    def test_weekly_review_lists_due_someday_items_and_gathers_topics(self):
        self.provider.topics = [{"name": "Sailing", "ids": []}]
        keeper = Housekeeper(self.agent)
        rows = {r["text"].split(" ·")[0]: r for r in keeper.someday_lines(self.agent.vault())}
        self.assertTrue(rows["Private thing #private"]["private"])
        self.provider.topics = [{"name": "Sailing", "ids": [rows[k]["id"] for k in
                                                              ("Sail the Cape", "Sailing course in Boston", "Buy a used dinghy")]}]
        keeper.weekly(self.agent.vault(), datetime(2026, 10, 4, 9, 0))
        note = self.read("01_GTD/REVIEWS/2026-W40.md")
        self.assertIn("### Someday to reconsider\n", note)
        self.assertIn("- Sail the Cape · review 2026-10-01 · [[SOMEDAY_MAYBE]]", note)
        self.assertNotIn("Buy a used dinghy · review", note.split("### Someday topics")[0])
        self.assertIn("- Sailing: 3 lines. Proposal in [[APPROVAL]]", note)
        sent = dict(self.provider.calls)["someday"]
        self.assertNotIn("Private thing #private", [line["text"] for line in sent])
        row = self.pending("someday")[0]
        self.agent.render_reviews()
        self.tick(proposal_id=row["id"])
        self.agent.sync_review_requests()
        project = self.read("02_PROJECTS/SAILING/SAILING.md")
        self.assertIn("status: someday", project)
        self.assertIn("review: 2026-10-01", project)
        self.assertIn("- Buy a used dinghy\n", project)
        someday = self.read("01_GTD/SOMEDAY_MAYBE.md")
        self.assertNotIn("Sail", someday)
        self.assertIn("- Learn to juggle", someday)


class ResearchTests(VaultCase):
    remote = True

    def setUp(self):
        super().setUp()
        config = self.settings.config_path
        config.write_text(config.read_text() + "[research]\nreports_dir = \"AGENT_INBOX\"\n", encoding="utf-8")
        from gtd_agent.core import Settings
        self.agent.settings = Settings.load(config)
        self.path("AGENT_INBOX").mkdir()

    def test_report_is_filed_into_reference_after_approval(self):
        self.path("_agent/RESEARCH_REQUESTS").mkdir(parents=True)
        self.write("_agent/RESEARCH_REQUESTS/DEMO_PROJECT.md", "---\ntype: research_request\nstatus: open\n---\n# R\n")
        self.write("AGENT_INBOX/lab report.md", "---\nrequest: DEMO_PROJECT\n---\n# How to launch a lab\n\nStep one.\n"
                                                "<!-- gtd-agent:proposal id=x -->\n")
        Housekeeper(self.agent).run()
        row = self.pending("research")[0]
        self.assertEqual(json.loads(row["proposal_json"])["title"], "File research report for DEMO_PROJECT")
        self.tick(proposal_id=row["id"])
        self.agent.sync_review_requests()
        note = self.read("04_REFERENCE/RESEARCH/DEMO_PROJECT_RESEARCH_2026-09-28.md")
        self.assertIn('project: ["[[DEMO_PROJECT]]"]\ntrust: external'.replace("\ntrust", "\norigin: agent\ntrust"), note)
        self.assertIn("# How to launch a lab\n\nStep one.", note)
        self.assertNotIn("gtd-agent:proposal", note)
        self.assertIn("Research report filed: [[DEMO_PROJECT_RESEARCH_2026-09-28]]", self.read(PROJECT))
        Housekeeper(self.agent).run()
        self.assertIn("status: done", self.read("_agent/RESEARCH_REQUESTS/DEMO_PROJECT.md"))
        self.assertEqual(self.pending("research"), [])  # the same report is not proposed twice


class WeeklyReviewTests(VaultCase):
    remote = True

    def test_created_on_review_day_then_left_alone_once_edited(self):
        keeper = Housekeeper(self.agent)
        self.assertEqual(keeper.weekly(self.agent.vault(), datetime(2026, 9, 28, 9, 0)), [])  # Monday: not review day
        self.provider.vague = [{"id": "x", "issue": "vague", "suggestion": "n/a"}]
        created = keeper.weekly(self.agent.vault(), datetime(2026, 10, 4, 9, 0))
        self.assertEqual(created, ["Created weekly review 2026-W40"])
        note = self.read("01_GTD/REVIEWS/2026-W40.md")
        self.assertIn("### Done this week (0)", note)
        self.assertIn("## Get clear", note)
        self.assertEqual([c[0] for c in self.provider.calls].count("vague"), 1)
        edited = note.replace("## This week at a glance (agent)", "## This week at a glance (agent) - mine")
        self.write("01_GTD/REVIEWS/2026-W40.md", edited)
        self.assertEqual(keeper.weekly(self.agent.vault(), datetime(2026, 10, 4, 12, 0), force=True), [])
        self.assertEqual(self.read("01_GTD/REVIEWS/2026-W40.md"), edited)
        self.assertEqual([c[0] for c in self.provider.calls].count("vague"), 1)

    def test_untouched_block_refreshes(self):
        keeper = Housekeeper(self.agent)
        keeper.weekly(self.agent.vault(), datetime(2026, 10, 4, 9, 0), force=True)
        self.write(PROJECT, self.read(PROJECT).replace("- [ ] Book the room #calls", "- [x] Book the room #calls ✅ 2026-09-29"))
        messages = keeper.weekly(self.agent.vault(), datetime(2026, 10, 4, 11, 0), force=True)
        self.assertEqual(messages, ["Refreshed weekly review 2026-W40"])
        note = self.read("01_GTD/REVIEWS/2026-W40.md")
        block = note[note.index(AUTO_BEGIN):note.index(AUTO_END)]
        self.assertIn("Book the room", block)

    def test_another_agent_copy_can_refresh_an_untouched_block(self):
        Housekeeper(self.agent).weekly(self.agent.vault(), datetime(2026, 10, 4, 9, 0), force=True)
        with self.agent.ledger.connection:  # a different agent copy: its ledger has no memory of this note
            self.agent.ledger.connection.execute("DELETE FROM kv")
        self.write(PROJECT, self.read(PROJECT).replace("- [ ] Book the room #calls", "- [x] Book the room #calls ✅ 2026-09-29"))
        self.assertEqual(Housekeeper(self.agent).weekly(self.agent.vault(), datetime(2026, 10, 4, 11, 0)),
                         ["Refreshed weekly review 2026-W40"])
        note = self.read("01_GTD/REVIEWS/2026-W40.md")
        self.assertRegex(note, r"<!-- gtd-agent:auto-end sha=[0-9a-f]{12} -->")


if __name__ == "__main__":
    unittest.main()
