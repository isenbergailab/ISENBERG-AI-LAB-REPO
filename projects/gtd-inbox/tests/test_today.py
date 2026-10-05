"""TODAY: a short report, then everything that needs you, then every next action by context."""
import json
import re
import unittest
from datetime import datetime

from helpers import TODAY, VaultCase
from gtd_agent.housekeeping import Housekeeper
from tasks_query import queries, select

LAB = "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"


class TodayCase(VaultCase):
    def extra_files(self):
        return {
            LAB: ("---\nkey: DEMO_PROJECT\ntype: project\nstatus: active\narea: \"[[AREA_CLUB]]\"\ndue: 2026-12-01\n"
                  "---\n# DEMO_PROJECT\n\n## Outcome\nLaunch the lab.\n\n## Steps\n"
                  "- [ ] Email the advisor #computer #next\n"
                  "- [ ] Send the budget #computer #next 📅 2026-09-25\n"
                  "- [ ] Confirm the speakers #calls #next 📅 2026-09-28\n"
                  "- [ ] Print the flyers #errands #next 🛫 2026-10-02\n"
                  "- [ ] Book the room #calls\n\n"
                  "## Waiting For\n- [ ] #waiting Admin office: room approval ➕ 2026-09-20 📅 2026-09-26\n\n"
                  "## Open questions\n- Which room should we book?\n- [ ] Who runs the first meeting?\n\n## Log\n"),
            "02_PROJECTS/LATIN/LATIN.md": ("---\nkey: LATIN\ntype: project\nstatus: someday\narea: \"[[AREA_CLUB]]\"\n"
                                           "review: 2026-09-20\n---\n# LATIN\n\n## Steps\n- [ ] Buy the textbook #errands\n\n"
                                           "## Open questions\n- Which textbook edition?\n"),
            "01_GTD/SOMEDAY_MAYBE.md": ("# SOMEDAY_MAYBE\n\n## Ideas\n- Learn to juggle\n"
                                        "- Sail the Cape · review 2026-09-27\n- Visit Rome · review 2027-05-01\n"),
        }

    def build(self):
        self.add_inbox("Buy stamps #errands")
        self.agent.scan()
        Housekeeper(self.agent).digest(self.agent.vault(), now=datetime(2026, 9, 28, 8, 0))
        return self.read("_agent/TODAY.md")

    def section(self, text, heading):
        match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
        self.assertIsNotNone(match, f"no section {heading}")
        return match.group(1)


class LayoutTests(TodayCase):
    def test_report_then_needs_you_then_next_actions(self):
        text = self.build()
        headings = re.findall(r"^## (.+)$", text, flags=re.M)
        self.assertEqual(headings, ["Report", "Needs you", "Next actions"])
        report = self.section(text, "Report")
        self.assertIn("[[INBOX]]: 1 line · [[APPROVAL]]: 1 waiting", report)
        self.assertIn("Calendar off", report)

    def test_every_needs_you_class_lands_in_its_section(self):
        needs = self.section(self.build(), "Needs you")
        subsections = re.findall(r"^### (.+)$", needs, flags=re.M)
        self.assertEqual(subsections, ["Overdue and due today (2)", "Follow-ups due (2)", "Open questions (2)",
                                       "Waiting in APPROVAL (1)", "Someday reviews due (2)"])
        self.assertIn("- [[DEMO_PROJECT#Open questions|DEMO_PROJECT]]: Which room should we book?", needs)
        self.assertIn("- [[DEMO_PROJECT#Open questions|DEMO_PROJECT]]: Who runs the first meeting?", needs)
        self.assertNotIn("Which textbook edition", needs)  # someday projects stay quiet
        proposal = self.pending("inbox")[0]
        self.assertIn(f"- [[APPROVAL#^p-{proposal['id']}|Next action · Buy stamps]] → Single action › @errands", needs)
        self.assertIn("- [[LATIN]] · review date Sun Sep 20", needs)
        self.assertIn("- Sail the Cape · review Sun Sep 27 · [[SOMEDAY_MAYBE]]", needs)
        self.assertNotIn("Visit Rome", needs)

    def test_the_approval_link_points_at_a_block_in_approval(self):
        self.build()
        proposal = self.pending("inbox")[0]
        self.assertIn(f"^p-{proposal['id']}", self.read("_agent/APPROVAL.md"))

    def test_next_actions_by_context_count_what_can_be_done_now(self):
        actions = self.section(self.build(), "Next actions")
        self.assertEqual(re.findall(r"^### (.+)$", actions, flags=re.M),
                         ["@computer (4)", "@calls (2)"])  # errands: flyers start Oct 2, stamps not approved yet


class QueryTests(TodayCase):
    """Each query selects the source tasks, so a tick in TODAY changes the line where the task lives."""

    def titles(self, heading):
        query = queries(self.build())[heading]
        return sorted(task.title for task in select(query, self.agent.vault().tasks, TODAY))

    def test_overdue_and_due_today(self):
        self.assertEqual(self.titles("Overdue and due today"), ["Confirm the speakers", "Send the budget"])

    def test_follow_ups_due(self):
        self.assertEqual(self.titles("Follow-ups due"), ["Admin office: room approval", "Riley: signed form AREA_ADMIN"])

    def test_calls_context(self):
        self.assertEqual(self.titles("@calls"), ["Call the dentist AREA_ADMIN", "Confirm the speakers"])

    def test_computer_context(self):
        self.assertEqual(self.titles("@computer"),
                         ["Download 1099 forms", "Email the advisor", "Renew library card", "Send the budget"])

    def test_a_project_named_like_an_agent_folder_still_shows(self):
        self.path("02_PROJECTS/GTD_AGENT").mkdir()
        self.write("02_PROJECTS/GTD_AGENT/GTD_AGENT.md", "---\nkey: GTD_AGENT\ntype: project\nstatus: active\n---\n"
                                                        "# GTD_AGENT\n\n## Steps\n- [ ] Reopen the assistant #computer #next\n")
        self.assertIn("Reopen the assistant", self.titles("@computer"))


if __name__ == "__main__":
    unittest.main()
