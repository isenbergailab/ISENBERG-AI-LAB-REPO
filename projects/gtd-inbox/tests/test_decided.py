import json

from helpers import VaultCase

DECISIONS = """# DEMO_PROJECT · Decisions

← [[DEMO_PROJECT]]

- **Name it The Demo** · short · 2026-09-20

## Budget
- **No dues this term** · free to join · 2026-09-21

## Open decisions
- [ ] Who advises the lab?
- Which room do we book?
"""


class DecidedSeparateNoteTest(VaultCase):
    remote = True

    def setUp(self):
        super().setUp()
        hub = self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md")
        hub = hub.replace("## Log", "## Decisions\nFull list: [[DEMO_PROJECT_DECISIONS]].\n\n## Open questions\n"
                                    "> [!question] Carried over\n> - Pick a meeting day?\n\n## Log", 1)
        self.write("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md", hub)
        self.write("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT_DECISIONS.md", DECISIONS)

    def test_decision_goes_to_the_separate_note_and_closes_the_answered_question(self):
        self.provider.decided_answers.append({
            "decision": "Oscar advises the lab", "why": "he agreed by email", "question_ids": ["q2"],
            "section": "", "steps": [{"title": "Email Oscar the meeting schedule", "context": "#computer"}],
            "explanation": "Settles the advisor question."})
        self.add_inbox("Decided: Oscar advises the lab because he agreed by email [[DEMO_PROJECT]]")
        self.agent.scan()
        row = self.pending("inbox")[0]
        proposal = json.loads(row["proposal_json"])
        self.assertEqual(proposal["kind"], "decision")
        self.assertEqual([item["checked"] for item in proposal["items"]], [False, True, False, True])
        self.assertEqual(self.provider.calls[-1][1]["sections"], ["Budget"])
        text = self.read("_agent/APPROVAL.md")
        self.assertIn("- [ ] 1. Remove question (Open questions): Pick a meeting day?", text)
        self.assertIn("- [x] 2. Close question (Open decisions): Who advises the lab?", text)
        self.assertNotIn("Teacher feedback", text.split(row["id"])[1].split("item-end")[0])
        self.tick("Approve", row["id"])
        self.agent.sync_review_requests()
        notes = self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT_DECISIONS.md")
        line = "- **Oscar advises the lab** · he agreed by email · 2026-09-28"
        self.assertIn(line, notes)
        self.assertLess(notes.index("Name it The Demo"), notes.index(line))
        self.assertLess(notes.index(line), notes.index("## Budget"))
        self.assertIn("- [x] Who advises the lab? ✅ 2026-09-28", notes)
        self.assertIn("- Which room do we book?", notes)
        hub = self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md")
        self.assertIn("> - Pick a meeting day?", hub)
        self.assertIn("- 2026-09-28 Decided: Oscar advises the lab", hub)
        self.assertIn("- [ ] Email Oscar the meeting schedule #computer", hub)
        self.assertNotIn("Decided:", self.read("00_INBOX/INBOX.md"))

    def test_unticking_and_ticking_items_changes_what_is_applied(self):
        self.provider.decided_answers.append({
            "decision": "We book room 101", "why": "", "question_ids": ["q3"], "section": "Budget", "steps": [],
            "explanation": ""})
        self.add_inbox("Decided: We book room 101 [[DEMO_PROJECT]]")
        self.agent.scan()
        row = self.pending("inbox")[0]
        text = self.read("_agent/APPROVAL.md")
        text = text.replace("- [ ] 1. Remove question", "- [x] 1. Remove question", 1)
        text = text.replace("- [x] 3. Remove question", "- [ ] 3. Remove question", 1)
        self.write("_agent/APPROVAL.md", text)
        self.tick("Approve", row["id"])
        self.agent.sync_review_requests()
        notes = self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT_DECISIONS.md")
        budget = notes.split("## Budget", 1)[1].split("## Open decisions", 1)[0]
        self.assertIn("- **We book room 101** · rationale not stated · 2026-09-28", budget)
        self.assertIn("- Which room do we book?", notes)  # unticked, so kept
        self.assertNotIn("Pick a meeting day?", self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"))


class DecidedHubOnlyTest(VaultCase):
    remote = False

    def test_without_a_model_the_hub_gets_a_decisions_section(self):
        self.add_inbox("Decided: File jointly · lower tax [[TAXES]]")
        self.agent.scan()
        row = self.pending("inbox")[0]
        self.assertEqual(self.provider.calls, [])
        self.tick("Approve", row["id"])
        self.agent.sync_review_requests()
        note = self.read("02_PROJECTS/TAXES/TAXES.md")
        self.assertIn("## Decisions\n- **File jointly** · lower tax · 2026-09-28", note)
        self.assertLess(note.index("## Decisions"), note.index("## Log"))

    def test_a_decision_needs_one_project_link(self):
        self.add_inbox("Decided: go to bed earlier")
        self.agent.scan()
        row = self.pending("inbox")[0]
        self.assertIn("exactly one project link", json.loads(row["proposal_json"])["reason"])
