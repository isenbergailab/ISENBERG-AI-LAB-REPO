"""Drop reasons: cancel a task and APPROVAL asks why; the answer is recorded where the task lived."""
import json
import re
import unittest
from datetime import datetime

from helpers import TODAY, VaultCase
from gtd_agent.core import REVIEW_BEGIN
from gtd_agent.feedback import BEGIN, END
from gtd_agent.housekeeping import Housekeeper

LAB = "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"
CLUB = "03_AREAS/AREA_CLUB.md"
SINGLES = "01_GTD/SINGLE_ACTIONS.md"


class DropCase(VaultCase):
    def extra_files(self):
        return {CLUB: ("---\nkey: AREA_CLUB\ntype: area\naliases: [Club]\nprivate: false\n---\n# AREA_CLUB\n\n"
                       "## Standard\nThe club runs well.\n\n## Recurring\n"
                       "- [ ] Send the weekly newsletter #computer #next 🔁 every week on Friday 📅 2026-10-02\n"
                       "\n## Projects\n```dataview\nLIST\n```\n")}

    def keeper(self):
        return Housekeeper(self.agent)

    def cancel(self, rel, line):
        text = self.read(rel)
        self.assertIn(line, text)
        self.write(rel, text.replace(line, line.replace("- [ ]", "- [-]", 1) + " ❌ 2026-09-28", 1))

    def answer(self, proposal_id, reason, choice="Save reason"):
        text = self.read("_agent/APPROVAL.md")
        start = text.index(f"<!-- gtd-agent:proposal id={proposal_id} -->")
        end = text.index("<!-- gtd-agent:item-end -->", start)
        block = text[start:end]
        field = block[block.index(BEGIN):block.index(END)]
        filled = re.sub(r"(\*\*Your reason:\*\*\n).*", lambda m: m.group(1) + f"> {reason}\n", field, flags=re.S)
        block = block.replace(field, filled).replace(f"- [ ] {choice}", f"- [x] {choice}", 1)
        self.write("_agent/APPROVAL.md", text[:start] + block + text[end:])
        return self.agent.sync_review_requests()

    def question(self):
        rows = self.pending("drop")
        self.assertEqual(len(rows), 1, [r["dedupe_key"] for r in rows])
        return rows[0]


class ProjectDropTests(DropCase):
    def test_a_cancelled_project_step_asks_why_and_records_the_answer(self):
        self.keeper().run(now=datetime(2026, 9, 28, 9, 0))  # first sight
        self.cancel(LAB, "- [ ] Book the room #calls")
        self.keeper().run(now=datetime(2026, 9, 28, 10, 0))
        row = self.question()
        proposal = json.loads(row["proposal_json"])
        self.assertEqual(proposal["title"], "Book the room")
        approval = self.read("_agent/APPROVAL.md")
        self.assertIn("### Why drop · Book the room", approval)
        self.assertIn("**Your reason:**", approval)
        outcomes = self.answer(row["id"], "no rooms free this term")
        self.assertTrue(any("Dropped" in o for o in outcomes), outcomes)
        lab = self.read(LAB)
        self.assertIn("- **Dropped: Book the room** · no rooms free this term · 2026-09-28", lab)
        self.assertIn("## Log\n- 2026-09-28 Dropped: Book the room · no rooms free this term", lab)
        self.assertLess(lab.index("## Decisions"), lab.index("## Log"))

    def test_dropping_the_last_next_step_promotes_the_first_open_one(self):
        self.keeper().run(now=datetime(2026, 9, 28, 9, 0))
        self.cancel(LAB, "- [ ] Email the advisor #computer #next")
        self.keeper().run(now=datetime(2026, 9, 28, 10, 0))
        self.assertIn("- [ ] Book the room #next #calls", self.read(LAB))

    def test_a_step_that_depended_on_the_dropped_one_gets_a_proposal(self):
        text = self.read(LAB).replace("- [ ] Book the room #calls", "- [ ] Book the room #calls 🆔 room1")
        text = text.replace("- [ ] Plan first meeting #computer", "- [ ] Plan first meeting #computer ⛔ room1")
        self.write(LAB, text)
        self.keeper().run(now=datetime(2026, 9, 28, 9, 0))
        self.cancel(LAB, "- [ ] Book the room #calls 🆔 room1")
        self.keeper().run(now=datetime(2026, 9, 28, 10, 0))
        knock = [json.loads(r["proposal_json"]) for r in self.pending("drop_knock")]
        self.assertEqual(len(knock), 1)
        self.assertIn("Plan first meeting", knock[0]["title"])
        self.tick("Approve", self.pending("drop_knock")[0]["id"])
        self.agent.sync_review_requests()
        self.assertIn("- [ ] Plan first meeting #computer\n", self.read(LAB))

    def test_skip_records_nothing_and_does_not_ask_again(self):
        self.keeper().run(now=datetime(2026, 9, 28, 9, 0))
        self.cancel(LAB, "- [ ] Book the room #calls")
        self.keeper().run(now=datetime(2026, 9, 28, 10, 0))
        row = self.question()
        self.tick("Skip", row["id"])
        self.agent.sync_review_requests()
        self.keeper().run(now=datetime(2026, 9, 28, 11, 0))
        self.assertEqual(self.pending("drop"), [])
        self.assertNotIn("Dropped", self.read(LAB))

    def test_un_cancelling_withdraws_the_question(self):
        self.keeper().run(now=datetime(2026, 9, 28, 9, 0))
        self.cancel(LAB, "- [ ] Book the room #calls")
        self.keeper().run(now=datetime(2026, 9, 28, 10, 0))
        self.question()
        self.write(LAB, self.read(LAB).replace("- [-] Book the room #calls ❌ 2026-09-28", "- [ ] Book the room #calls"))
        self.keeper().run(now=datetime(2026, 9, 28, 11, 0))
        self.assertEqual(self.pending("drop"), [])

    def test_today_lists_the_questions_apart_from_other_proposals(self):
        self.keeper().run(now=datetime(2026, 9, 28, 9, 0))
        self.cancel(LAB, "- [ ] Book the room #calls")
        self.keeper().run(now=datetime(2026, 9, 28, 10, 0))
        row = self.question()
        today = self.read("_agent/TODAY.md")
        self.assertIn("### Drop reasons asked (1)", today)
        self.assertIn(f"- [[APPROVAL#^p-{row['id']}|Why drop · Book the room]]", today)
        waiting = today.split("### Waiting in APPROVAL")[1] if "### Waiting in APPROVAL" in today else ""
        self.assertNotIn("Book the room", waiting.split("###")[0])


class OtherDropTests(DropCase):
    def test_a_dropped_area_duty_is_recorded_in_the_area_note(self):
        self.keeper().run(now=datetime(2026, 9, 28, 9, 0))
        self.cancel(CLUB, "- [ ] Send the weekly newsletter #computer #next 🔁 every week on Friday 📅 2026-10-02")
        self.keeper().run(now=datetime(2026, 9, 28, 10, 0))
        self.answer(self.question()["id"], "the club moved to Slack")
        club = self.read(CLUB)
        self.assertTrue(club.rstrip().endswith(
            "## Decisions\n- **Dropped: Send the weekly newsletter** · the club moved to Slack · 2026-09-28\n\n"
            "## Log\n- 2026-09-28 Dropped: Send the weekly newsletter · the club moved to Slack"), club)

    def test_a_single_action_goes_to_its_linked_area_or_else_to_single_actions(self):
        self.keeper().run(now=datetime(2026, 9, 28, 9, 0))
        self.cancel(SINGLES, "- [ ] Renew library card #computer")
        self.keeper().run(now=datetime(2026, 9, 28, 10, 0))
        self.answer(self.question()["id"], "moved away")
        singles = self.read(SINGLES)
        self.assertIn("## Decisions\n- **Dropped: Renew library card** · moved away · 2026-09-28", singles)
        self.assertIn("## Log\n- 2026-09-28 Dropped: Renew library card · moved away", singles)

    def test_a_cancel_from_before_the_feature_is_left_alone(self):
        self.write(SINGLES, self.read(SINGLES).replace("- [ ] Renew library card #computer",
                                                       "- [-] Renew library card #computer ❌ 2026-09-20"))
        self.keeper().run(now=datetime(2026, 9, 28, 9, 0))
        self.keeper().run(now=datetime(2026, 9, 28, 10, 0))
        self.assertEqual(self.pending("drop"), [])

    def test_a_task_the_agent_cancels_itself_asks_nothing(self):
        self.keeper().run(now=datetime(2026, 9, 28, 9, 0))
        vault = self.agent.vault()
        task = next(t for t in vault.tasks if t.title == "Renew library card")
        self.agent.note_agent_cancel(task)
        self.cancel(SINGLES, "- [ ] Renew library card #computer")
        self.keeper().run(now=datetime(2026, 9, 28, 10, 0))
        self.assertEqual(self.pending("drop"), [])


if __name__ == "__main__":
    unittest.main()
