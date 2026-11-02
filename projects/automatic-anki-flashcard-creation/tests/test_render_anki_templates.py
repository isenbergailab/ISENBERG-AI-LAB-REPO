from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
MODULE_PATH = SCRIPTS / "render_anki_templates.py"
SPEC = importlib.util.spec_from_file_location("render_anki_templates", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RenderTemplatesTests(unittest.TestCase):
    def test_custom_field_names_are_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = json.loads((ROOT / "config" / "example.json").read_text(encoding="utf-8"))
            config["field_mapping"] = {
                "Prompt": "Word",
                "Answer": "Meaning",
                "Notes": "Pronunciation",
                "Example": "Sentence",
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "rendered"
            paths = MODULE.render_templates(config_path, output)
            self.assertEqual(len(paths), 3)
            front = (output / "front.html").read_text(encoding="utf-8")
            back = (output / "back.html").read_text(encoding="utf-8")
            self.assertIn("{{Word}}", front)
            self.assertIn("{{Meaning}}", back)
            self.assertIn("{{#Pronunciation}}", back)
            self.assertIn("{{Sentence}}", back)


if __name__ == "__main__":
    unittest.main()
