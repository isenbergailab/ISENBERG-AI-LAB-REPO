from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "validate_package.py"
SPEC = importlib.util.spec_from_file_location("validate_package", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PackageTests(unittest.TestCase):
    def test_package_structure(self) -> None:
        self.assertEqual(MODULE.validate_package(ROOT), [])

    def test_skill_description_is_short(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        line = next(line for line in skill.splitlines() if line.startswith("description:"))
        description = line.split(":", 1)[1].strip().strip('"')
        self.assertLessEqual(len(description), 60)
        self.assertTrue(description.endswith("."))


if __name__ == "__main__":
    unittest.main()
