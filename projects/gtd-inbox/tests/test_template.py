"""Project notes follow the VAULT_RULES template, whoever adds a section, and agent-written tasks stay plain."""
import json
import re
import unittest
from datetime import datetime

from helpers import TODAY, VaultCase, draft
from gtd_agent.housekeeping import Housekeeper
from gtd_agent.ops import apply_op
from test_documents import BRIEF, DocumentCase

# The order VAULT_RULES gives for a project note, written out here as the spec.
VAULT_RULES_ORDER = ["Outcome", "Why", "Context", "Current state", "Steps", "Deadlines", "Waiting For", "Decisions",
                     "Open questions", "Notes", "Log", "Links"]
PATH = "02_PROJECTS/DEMO/DEMO.md"


def headings(text):
    return re.findall(r"^## (.+)$", text, flags=re.M)


def note(sections):
    body = "".join(f"## {name}\n- {name} content\n\n" for name in sections)
    return f"---\nkey: DEMO\ntype: project\nstatus: active\n---\n# DEMO\n\n{body}"


def add_to(text, section):
    return apply_op(text, {"op": "append_bullet", "path": PATH, "section": section, "text": f"added to {section}"},
                    TODAY)


class SectionOrderTests(unittest.TestCase):
    def test_each_missing_section_lands_in_vault_rules_order(self):
        for missing in VAULT_RULES_ORDER:
            with self.subTest(missing=missing):
                others = [name for name in VAULT_RULES_ORDER if name != missing]
                self.assertEqual(headings(add_to(note(others), missing)), VAULT_RULES_ORDER)

    def test_sections_find_their_place_in_a_sparse_note(self):
        text = note(["Outcome", "Steps", "Log"])
        text = add_to(text, "Current state")
        self.assertEqual(headings(text), ["Outcome", "Current state", "Steps", "Log"])
        text = add_to(text, "Decisions")
        self.assertEqual(headings(text), ["Outcome", "Current state", "Steps", "Decisions", "Log"])
        text = add_to(text, "Why")
        self.assertEqual(headings(text), ["Outcome", "Why", "Current state", "Steps", "Decisions", "Log"])
        self.assertIn("## Why\n- added to Why\n\n## Current state", text)


class NewProjectTemplateTests(VaultCase):
    remote = True

    def test_a_new_project_from_the_inbox_follows_the_template(self):
        self.add_inbox("new project: Biology 101 notes, first read chapter 1")
        self.provider.interpretations.append(draft(
            route="new_project", name_excerpt="Biology 101", title="Organize Biology 101 notes",
            first_step="read chapter 1", area_key="AREA_CLUB", context="#anywhere"))
        text = self.agent.scan().new_proposals[0]["proposal"]["ops"][0]["text"]
        self.assertEqual(headings(text), VAULT_RULES_ORDER)
        self.assertIn("## Outcome\nOrganize Biology 101 notes\n", text)
        self.assertIn("## Steps\n- [ ] read chapter 1 #anywhere #next ➕ 2026-09-28\n", text)
        self.assertIn("## Notes\n### Original Inbox capture\n> new project: Biology 101 notes", text)

    def test_model_words_never_become_a_next_or_waiting_tag(self):
        self.add_inbox("explain the next tag to members")
        self.provider.interpretations.append(draft(title="Explain the #next tag to members", project_key="DEMO_PROJECT"))
        step = self.agent.scan().new_proposals[0]["proposal"]["ops"][0]["line"]
        self.assertIn("Explain the next tag to members", step)
        self.assertNotIn("#next", step)  # DEMO_PROJECT already has a next step, so this one queues
        self.add_inbox("Sam owes me the next draft")
        self.provider.interpretations.append(draft(route="waiting_for", person="Sam", what="the #next draft"))
        wait = [p for p in self.agent.scan().new_proposals if p["proposal"]["kind"] == "waiting_for"][0]
        line = wait["proposal"]["ops"][0]["line"]
        self.assertTrue(line.startswith("- [ ] #waiting Sam: the next draft"), line)
        self.assertNotIn("#next", line)


class BriefTemplateTests(DocumentCase):
    def test_a_project_brief_creates_a_note_in_template_order(self):
        self.provider.documents.append({
            "doc_type": "notes", "title": "Calendar and Gmail integrations", "course": "", "target_key": "",
            "reference_folder": "", "summary": "Plan.", "facts": [], "deadlines": [],
            "actions": [{"title": "Create a GTD label in Gmail", "context": "#computer", "due": "",
                         "evidence": "Create a GTD label in Gmail"}], "explanation": "A project brief."})
        self.drop("calendar brief.md", BRIEF)
        self.run_housekeeping()
        proposal = json.loads(self.pending("document")[0]["proposal_json"])
        created = next(op for op in proposal["base_ops"] if op["path"].startswith("02_PROJECTS/"))
        self.assertEqual(headings(created["text"]), VAULT_RULES_ORDER)


class SomedayTopicTemplateTests(VaultCase):
    remote = True

    def extra_files(self):
        return {"01_GTD/SOMEDAY_MAYBE.md": "# SOMEDAY_MAYBE\n\n## Ideas\n- Sail the Cape\n- Sailing course in Boston\n\n"
                                          "## Added from the Inbox\n- Buy a used dinghy\n"}

    def test_a_gathered_someday_topic_becomes_a_note_in_template_order(self):
        keeper = Housekeeper(self.agent)
        ids = [row["id"] for row in keeper.someday_lines(self.agent.vault())]
        self.provider.topics = [{"name": "Sailing", "ids": ids}]
        keeper.weekly(self.agent.vault(), datetime(2026, 10, 4, 9, 0))
        text = json.loads(self.pending("someday")[0]["proposal_json"])["ops"][0]["text"]
        self.assertEqual(headings(text), VAULT_RULES_ORDER)
        self.assertIn("- Buy a used dinghy\n", text)


if __name__ == "__main__":
    unittest.main()
