import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from college_club_admin.campaigns import build_campaign, write_package
from college_club_admin.ledger import transition
from college_club_admin.transcripts import review_transcript


def example(name):
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))


class CampaignTests(unittest.TestCase):
    def test_weekly_package_is_reviewable_and_x_is_manual(self):
        campaign = build_campaign("weekly", example("weekly.json"))
        with tempfile.TemporaryDirectory() as temporary:
            folder = write_package(campaign, Path(temporary), ROOT / "assets")
            self.assertTrue((folder / "instagram-card.png").exists())
            self.assertTrue((folder / "instagram-story.png").exists())
            self.assertTrue((folder / "x-image.png").exists())
            self.assertTrue((folder / "posting-checklist.md").exists())
            self.assertEqual(campaign["state"], "draft")
            self.assertLessEqual(len((folder / "x-post.txt").read_text().strip()), 280)
            self.assertIn("5:15–6", campaign["copy"]["slack"])

    def test_unmerged_project_cannot_create_campaign(self):
        brief = example("merge.json")
        brief["merged"] = False
        with self.assertRaises(ValueError):
            build_campaign("merge", brief)

    def test_member_contact_information_blocks_draft(self):
        brief = example("weekly.json")
        brief["topics"] = ["contact member@example.com"]
        with self.assertRaises(ValueError):
            build_campaign("weekly", brief)

    def test_approval_controls_publication(self):
        campaign = build_campaign("merge", example("merge.json"))
        with self.assertRaises(ValueError):
            transition(campaign, "published")
        transition(campaign, "awaiting_approval")
        transition(campaign, "approved", actor="president-slack-id")
        transition(campaign, "published", permalink="https://x.com/example/status/1")
        self.assertEqual(campaign["publication"]["x"], "https://x.com/example/status/1")

    def test_monthly_report_omits_private_source_links_and_excluded_sections(self):
        brief = example("monthly.json")
        brief["sources"].append({"kind": "approved_summary", "url": "https://drive.google.com/file/d/private/view", "claim": "Reviewed internal record"})
        report = build_campaign("monthly", brief)["copy"]["substack"]
        self.assertIn("## Shipped projects", report)
        self.assertNotIn("drive.google.com", report)
        self.assertNotIn("Next month", report)
        self.assertNotIn("Spending", report)

    def test_sensitive_transcript_cannot_feed_public_copy(self):
        result = review_transcript("Contact member@example.com about password details")
        self.assertEqual(result["status"], "blocked_private_review")
        self.assertFalse(result["public_use"])
        self.assertEqual(result["approved_summary"], "")


if __name__ == "__main__":
    unittest.main()
