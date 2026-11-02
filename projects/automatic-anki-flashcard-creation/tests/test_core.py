from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
MODULE_PATH = SCRIPTS / "automatic_anki_core.py"
SPEC = importlib.util.spec_from_file_location("automatic_anki_core", MODULE_PATH)
assert SPEC and SPEC.loader
CORE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CORE
SPEC.loader.exec_module(CORE)

FIXTURES = ROOT / "fixtures" / "sources"


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "config.json"
        self.write_config()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_config(self, policy: str = "reject", **overrides: object) -> None:
        config = {
            "target_language": "ja",
            "explanation_language": "en",
            "language_profile": "japanese",
            "profile_options": {},
            "field_mapping": {
                "Prompt": "Front",
                "Answer": "Back",
                "Notes": "Notes",
                "Example": "Example",
            },
            "batch": {"explicit_export": True, "inactivity_seconds": 0},
            "duplicates": {"policy": policy, "case_sensitive": False},
            "storage": {
                "state_directory": "state",
                "identity_scope": "user-and-conversation",
            },
            "output": {
                "directory": "exports",
                "naming": "nato",
                "write_validation_report": True,
            },
            "adapter": {"type": "hermes"},
        }
        config.update(overrides)
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )

    def add(self, fixture: str, user: str = "user-1", conversation: str = "chat-1") -> dict:
        return CORE.add_candidate(
            self.config_path, "hermes", user, conversation, FIXTURES / fixture
        )

    def export(self, user: str = "user-1", conversation: str = "chat-1") -> dict:
        return CORE.export_batch(
            self.config_path, "hermes", user, conversation
        )

    def test_text_and_image_sources_export(self) -> None:
        self.assertEqual(self.add("text-cat.json")["status"], "added")
        self.assertEqual(self.add("image-eat.json")["status"], "added")
        result = self.export()
        self.assertEqual(result["card_count"], 2)
        self.assertEqual(len(result["media"]), 2)
        self.assertTrue(all(line.startswith("MEDIA:") for line in result["media"]))
        tsv = Path(result["tsv"]).read_text(encoding="utf-8")
        self.assertIn("猫\tcat\tReading: ねこ", tsv)
        self.assertIn("食べる\tto eat\tReading: たべる", tsv)

    def test_ambiguous_source_is_rejected_and_reported(self) -> None:
        result = self.add("ambiguous-image.json")
        self.assertEqual(result["status"], "rejected")
        self.add("text-cat.json")
        exported = self.export()
        report = json.loads(Path(exported["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["rejected_sources"][0]["source_id"], "ambiguous-page-1")

    def test_malformed_source_fails_without_card(self) -> None:
        with self.assertRaises(CORE.CoreError):
            self.add("malformed.json")
        with self.assertRaisesRegex(CORE.CoreError, "no cards"):
            self.export()

    def test_duplicate_reject(self) -> None:
        self.add("text-cat.json")
        result = self.add("duplicate-cat.json")
        self.assertEqual(result["status"], "duplicate-rejected")
        exported = self.export()
        self.assertEqual(exported["card_count"], 1)
        report = json.loads(Path(exported["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["batch"]["duplicate_outcomes"][0]["outcome"], "rejected")

    def test_duplicate_allow(self) -> None:
        self.write_config(policy="allow")
        self.add("text-cat.json")
        self.add("duplicate-cat.json")
        exported = self.export()
        self.assertEqual(exported["card_count"], 2)

    def test_duplicate_merge_records_conflicts(self) -> None:
        self.write_config(policy="merge")
        self.add("text-cat.json")
        result = self.add("duplicate-cat.json")
        self.assertEqual(result["status"], "duplicate-merged")
        exported = self.export()
        report = json.loads(Path(exported["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["batch"]["duplicate_outcomes"][0]["outcome"], "merged-with-conflicts")
        self.assertTrue(report["cards"][0]["review_required"])

    def test_identity_scopes_separate_users_and_sequences(self) -> None:
        self.add("text-cat.json", user="alice", conversation="one")
        self.add("image-eat.json", user="bob", conversation="one")
        alice = self.export(user="alice", conversation="one")
        bob = self.export(user="bob", conversation="one")
        self.assertEqual(Path(alice["tsv"]).name, "Alpha.tsv")
        self.assertEqual(Path(bob["tsv"]).name, "Alpha.tsv")
        self.assertNotEqual(Path(alice["tsv"]).parent, Path(bob["tsv"]).parent)

    def test_spanish_profile_fixture(self) -> None:
        self.write_config(target_language="es", language_profile="spanish")
        self.add("spanish-casa.json")
        exported = self.export()
        report = json.loads(Path(exported["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["language_profile"], "Spanish vocabulary")

    def test_mandarin_profile_fixture(self) -> None:
        self.write_config(
            target_language="zh-Hans",
            language_profile="mandarin",
            profile_options={"script": "simplified", "pinyin_style": "tone-marks"},
        )
        self.add("mandarin-cat.json")
        exported = self.export()
        report = json.loads(Path(exported["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["language_profile"], "Mandarin Chinese vocabulary")

    def test_source_html_is_escaped_and_only_controlled_br_remains(self) -> None:
        self.add("html-injection.json")
        exported = self.export()
        tsv = Path(exported["tsv"]).read_text(encoding="utf-8")
        self.assertNotIn("<script>", tsv)
        self.assertNotIn("<img", tsv)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", tsv)
        self.assertIn("<br>", tsv)

    def test_config_requires_distinct_field_mapping(self) -> None:
        duplicate_mapping = {
            "Prompt": "Field",
            "Answer": "Field",
            "Notes": "Field",
            "Example": "Field",
        }
        self.write_config(field_mapping=duplicate_mapping)
        with self.assertRaisesRegex(CORE.CoreError, "distinct"):
            CORE.load_runtime(self.config_path)

    def test_config_and_profile_languages_must_match(self) -> None:
        self.write_config(target_language="es")
        with self.assertRaisesRegex(CORE.CoreError, "target_language"):
            CORE.load_runtime(self.config_path)

    def test_atomic_reservation_advances_after_existing_pair(self) -> None:
        directory = self.root / "reserve"
        first_tsv, first_report, first_tsv_fd, first_report_fd = CORE.reserve_pair(directory)
        os.close(first_tsv_fd)
        os.close(first_report_fd)
        second_tsv, second_report, second_tsv_fd, second_report_fd = CORE.reserve_pair(directory)
        os.close(second_tsv_fd)
        os.close(second_report_fd)
        self.assertEqual(first_tsv.name, "Alpha.tsv")
        self.assertEqual(first_report.name, "Alpha.validation.json")
        self.assertEqual(second_tsv.name, "Bravo.tsv")
        self.assertEqual(second_report.name, "Bravo.validation.json")

    def test_inactivity_export_requires_enabled_timeout(self) -> None:
        self.add("text-cat.json")
        with self.assertRaisesRegex(CORE.CoreError, "disabled"):
            CORE.export_batch(
                self.config_path, "hermes", "user-1", "chat-1", "inactivity"
            )

    def test_inactivity_export_records_finalizer_when_enabled(self) -> None:
        self.write_config(
            batch={"explicit_export": True, "inactivity_seconds": 300}
        )
        self.add("text-cat.json")
        exported = CORE.export_batch(
            self.config_path, "hermes", "user-1", "chat-1", "inactivity"
        )
        report = json.loads(Path(exported["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["batch"]["finalized_by"], "inactivity")

    def test_initialize_config_creates_profile_local_files(self) -> None:
        home = self.root / "hermes-home"
        destination = CORE.initialize_config(home)
        self.assertEqual(
            destination,
            home.resolve() / "automatic-anki-flashcard-creation" / "config.json",
        )
        self.assertTrue((destination.parent / "state").is_dir())
        self.assertTrue((destination.parent / "exports").is_dir())
        CORE.load_runtime(destination)


if __name__ == "__main__":
    unittest.main()
