"""TOPIC links require approval and never expose or edit private source notes."""
from helpers import VaultCase
from gtd_agent.reference_links import ReferenceLinker


class FarLinksTests(VaultCase):
    def seed(self):
        self.path("02_PROJECTS/TOPIC_EXAM_STUDY").mkdir(parents=True, exist_ok=True)
        self.path("04_REFERENCE/TOPIC").mkdir(parents=True, exist_ok=True)
        self.write("02_PROJECTS/TOPIC_EXAM_STUDY/TOPIC_EXAM_STUDY.md",
                   "---\nkey: TOPIC_EXAM_STUDY\ntype: project\nstatus: active\n---\n"
                   "# TOPIC_EXAM_STUDY\n\n## Links\n\nManual link stays.\n")
        self.write("04_REFERENCE/TOPIC/TOPIC_INDEX.md", "# TOPIC_INDEX\n\nMy own notes stay.\n")
        self.write("04_REFERENCE/TOPIC/TOPIC_CHAPTER_1.md", "# TOPIC chapter 1\n")
        self.write("04_REFERENCE/TOPIC/PRIVATE_TITLE.md", "---\nprivate: true\n---\n# Private\n")
        self.write("04_REFERENCE/TOPIC/SHARES_PRIVATE_TITLE.md", "# Link\n[[PRIVATE_TITLE]]\n")

    def test_proposal_links_public_notes_only_after_approval(self):
        self.seed()
        messages = ReferenceLinker(self.agent).run(self.agent.vault())
        self.assertEqual(len(messages), 1)
        proposal = self.pending("reference")[0]
        encoded = proposal["proposal_json"]
        self.assertIn("TOPIC_CHAPTER_1", encoded)
        self.assertNotIn("PRIVATE_TITLE", encoded)
        self.assertNotIn("[[TOPIC_INDEX]]", self.read("02_PROJECTS/TOPIC_EXAM_STUDY/TOPIC_EXAM_STUDY.md"))
        self.agent.render_reviews()
        self.tick()
        self.agent.sync_review_requests()
        index = self.read("04_REFERENCE/TOPIC/TOPIC_INDEX.md")
        self.assertIn("My own notes stay.", index)
        self.assertIn("[[04_REFERENCE/TOPIC/TOPIC_CHAPTER_1]]", index)
        self.assertNotIn("PRIVATE_TITLE", index)
        self.assertIn("[[TOPIC_INDEX]]", self.read("02_PROJECTS/TOPIC_EXAM_STUDY/TOPIC_EXAM_STUDY.md"))
        self.assertEqual(self.read("04_REFERENCE/TOPIC/TOPIC_CHAPTER_1.md"), "# TOPIC chapter 1\n")

    def test_addition_requires_new_approval(self):
        self.seed()
        ReferenceLinker(self.agent).run(self.agent.vault())
        self.agent.render_reviews()
        self.tick()
        self.agent.sync_review_requests()
        self.write("04_REFERENCE/TOPIC/TOPIC_CHAPTER_2.md", "# TOPIC chapter 2\n")
        ReferenceLinker(self.agent).run(self.agent.vault())
        self.assertEqual(len(self.pending("reference")), 1)
        self.assertNotIn("TOPIC_CHAPTER_2", self.read("04_REFERENCE/TOPIC/TOPIC_INDEX.md"))

    def test_uncertain_match_asks_for_project_then_previews_links(self):
        self.path("04_REFERENCE/TOPIC").mkdir(parents=True, exist_ok=True)
        self.write("04_REFERENCE/TOPIC/TOPIC_INDEX.md", "# TOPIC_INDEX\n")
        self.write("04_REFERENCE/TOPIC/TOPIC_CHAPTER_1.md", "# Chapter 1\n")
        ReferenceLinker(self.agent).run(self.agent.vault())
        row = self.pending("reference")[0]
        self.assertIn("reference_match", row["proposal_json"])
        self.agent.save_reference_project(row["id"], "DEMO_PROJECT")
        self.assertNotIn("TOPIC_CHAPTER_1", self.read("04_REFERENCE/TOPIC/TOPIC_INDEX.md"))
        self.assertIn("Change:\n<pre>", self.read("_agent/APPROVAL.md"))
        self.tick()
        self.agent.sync_review_requests()
        self.assertIn("TOPIC_CHAPTER_1", self.read("04_REFERENCE/TOPIC/TOPIC_INDEX.md"))
        self.assertEqual(self.agent.ledger.kv_get("reference_project|TOPIC"), "DEMO_PROJECT")

    def test_existing_manual_index_link_is_not_duplicated(self):
        self.seed()
        self.write("04_REFERENCE/TOPIC/TOPIC_INDEX.md",
                   "# TOPIC_INDEX\n\nMy own link: [[TOPIC_CHAPTER_1]].\n")
        ReferenceLinker(self.agent).run(self.agent.vault())
        self.agent.render_reviews()
        self.tick()
        self.agent.sync_review_requests()
        index = self.read("04_REFERENCE/TOPIC/TOPIC_INDEX.md")
        self.assertEqual(index.count("TOPIC_CHAPTER_1"), 1)
        self.assertIn("My own link:", index)
