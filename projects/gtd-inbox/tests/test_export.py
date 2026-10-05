"""Item 15: `export KEY` bundles a project into one Markdown file in state/exports/."""
import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime

from helpers import VaultCase
from gtd_agent.cli import main
from gtd_agent.export import export_project

NOW = datetime(2026, 9, 28, 9, 0)
BOAT = "02_PROJECTS/BOAT/BOAT.md"
NOTES = {
    BOAT: "---\nkey: BOAT\ntype: project\nstatus: active\narea: \"[[AREA_CLUB]]\"\nprivate: false\n---\n# BOAT\n\n"
          "## Outcome\nA boat on the lake.\n\n## Context\nSee [[SAIL_NOTES]], [[SAIL_NOTES|the notes]] and "
          "[[BOAT_DECISIONS]].\n\n## Links\n- [[HARBOR]]\n- [[Missing Note]]\n",
    "02_PROJECTS/BOAT/BOAT_DECISIONS.md": "# BOAT_DECISIONS\n\n## Hull\n- **Wood** · warm · 2026-09-01\n"
                                          "Details in [[SAIL_NOTES]].\n",
    "04_REFERENCE/NOTES/SAIL_NOTES.md": "# SAIL_NOTES\n\n## Knots\nBowline first.\n",
    "04_REFERENCE/NOTES/HARBOR.md": "---\nproject: \"[[BOAT]]\"\n---\n# HARBOR\nSlip 12.\n",
    "04_REFERENCE/NOTES/ENGINE.md": "---\nproject: [\"[[DEMO_PROJECT]]\", \"[[BOAT]]\"]\n---\n# ENGINE\nTwo stroke.\n",
}
INCLUDED = [BOAT, "02_PROJECTS/BOAT/BOAT_DECISIONS.md", "03_AREAS/AREA_CLUB.md", "04_REFERENCE/NOTES/SAIL_NOTES.md",
            "04_REFERENCE/NOTES/HARBOR.md", "04_REFERENCE/NOTES/ENGINE.md"]


class ExportTests(VaultCase):
    def extra_files(self):
        return NOTES

    def test_every_linked_note_appears_exactly_once(self):
        path = export_project(self.settings, "BOAT", NOW)
        self.assertEqual(path, self.settings.state_dir / "exports" / "BOAT-2026-09-28.md")
        text = path.read_text(encoding="utf-8")
        for rel in INCLUDED:
            self.assertEqual(text.count(f"`{rel}`"), 1, rel)
        self.assertNotIn("GUIDE.md", text)  # links to the project, but the project does not link it
        self.assertLess(text.index(f"`{BOAT}`"), text.index("`02_PROJECTS/BOAT/BOAT_DECISIONS.md`"))
        self.assertIn("#### Knots", text)  # each note's headings sit under its own
        self.assertIn("```yaml\nproject: \"[[BOAT]]\"\n```", text)
        self.assertNotIn("Private", text)

    def test_a_private_project_exports_with_a_banner(self):
        text = export_project(self.settings, "TAXES", NOW).read_text(encoding="utf-8")
        self.assertTrue(text.split("\n")[2].startswith("> [!warning] Private"))

    def test_the_command_prints_where_the_file_went(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--config", str(self.settings.config_path), "export", "boat"])
        self.assertEqual(code, 0)
        self.assertIn("BOAT-", out.getvalue())
        self.assertIn("6 notes", out.getvalue())

    def test_an_unknown_key_says_so(self):
        with self.assertRaisesRegex(ValueError, "No project named NOPE"):
            export_project(self.settings, "NOPE", NOW)


if __name__ == "__main__":
    unittest.main()
