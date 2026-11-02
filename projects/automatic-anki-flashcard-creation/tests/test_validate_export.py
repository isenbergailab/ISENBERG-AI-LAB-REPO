from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_export.py"
SPEC = importlib.util.spec_from_file_location("validate_export", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def report_for(filename: str, prompts: list[str]) -> dict:
    return {
        "schema_version": 1,
        "export_filename": filename,
        "card_count": len(prompts),
        "language_profile": "Test",
        "batch": {
            "adapter": "hermes",
            "identity": "hermes-testidentity01",
            "identity_scope": "user-and-conversation",
            "finalized_by": "explicit",
            "duplicate_policy": "reject",
            "duplicate_outcomes": [],
        },
        "cards": [
            {
                "row": number,
                "source_id": f"source-{number}",
                "source_type": "text",
                "prompt": prompt,
                "provenance": {
                    "Prompt": "source",
                    "Answer": "source",
                    "Notes": "source",
                    "Example": "generated",
                },
                "uncertain_fields": [],
                "review_required": True,
                "issues": ["Generated example"],
            }
            for number, prompt in enumerate(prompts, start=1)
        ],
        "rejected_sources": [],
    }


class ValidateExportTests(unittest.TestCase):
    def write_export(
        self,
        directory: Path,
        tsv: str,
        report: dict,
        report_name: str = "Alpha.validation.json",
    ) -> tuple[Path, Path]:
        tsv_path = directory / "Alpha.tsv"
        report_path = directory / report_name
        tsv_path.write_text(tsv, encoding="utf-8", newline="\n")
        report_path.write_text(json.dumps(report), encoding="utf-8", newline="\n")
        return tsv_path, report_path

    def test_valid_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tsv, report = self.write_export(
                Path(directory),
                "猫\tcat\tReading\tExample<br>Translation\n",
                report_for("Alpha.tsv", ["猫"]),
            )
            result = MODULE.validate_export(tsv, report, expected_count=1)
            self.assertEqual(result.rows, 1)

    def test_exactly_three_separators_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tsv, report = self.write_export(
                Path(directory),
                "猫\tcat\tReading\n",
                report_for("Alpha.tsv", ["猫"]),
            )
            with self.assertRaisesRegex(ValueError, "expected 3"):
                MODULE.validate_export(tsv, report)

    def test_quoted_internal_tab_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tsv, report = self.write_export(
                Path(directory),
                '"猫\tねこ"\tcat\tReading\tExample\n',
                report_for("Alpha.tsv", ['"猫\tねこ"']),
            )
            with self.assertRaisesRegex(ValueError, "expected 3"):
                MODULE.validate_export(tsv, report)

    def test_expected_count_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tsv, report = self.write_export(
                Path(directory),
                "猫\tcat\tReading\tExample\n",
                report_for("Alpha.tsv", ["猫"]),
            )
            with self.assertRaisesRegex(ValueError, "expected 2"):
                MODULE.validate_export(tsv, report, expected_count=2)

    def test_report_export_filename_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tsv, report = self.write_export(
                Path(directory),
                "猫\tcat\tReading\tExample\n",
                report_for("Bravo.tsv", ["猫"]),
            )
            with self.assertRaisesRegex(ValueError, "export_filename"):
                MODULE.validate_export(tsv, report)

    def test_report_companion_filename_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tsv, report = self.write_export(
                Path(directory),
                "猫\tcat\tReading\tExample\n",
                report_for("Alpha.tsv", ["猫"]),
                report_name="wrong.json",
            )
            with self.assertRaisesRegex(ValueError, "companion"):
                MODULE.validate_export(tsv, report)

    def test_report_prompt_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tsv, report = self.write_export(
                Path(directory),
                "猫\tcat\tReading\tExample\n",
                report_for("Alpha.tsv", ["犬"]),
            )
            with self.assertRaisesRegex(ValueError, "prompt mismatch"):
                MODULE.validate_export(tsv, report)

    def test_missing_source_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_data = report_for("Alpha.tsv", ["猫"])
            report_data["cards"][0]["source_id"] = ""
            tsv, report = self.write_export(
                Path(directory),
                "猫\tcat\tReading\tExample\n",
                report_data,
            )
            with self.assertRaisesRegex(ValueError, "source_id"):
                MODULE.validate_export(tsv, report)

    def test_rejected_source_structure_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_data = report_for("Alpha.tsv", ["猫"])
            report_data["rejected_sources"] = [{"source_id": "bad"}]
            tsv, report = self.write_export(
                Path(directory),
                "猫\tcat\tReading\tExample\n",
                report_data,
            )
            with self.assertRaisesRegex(ValueError, "rejected source"):
                MODULE.validate_export(tsv, report)

    def test_byte_order_mark_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tsv, report = self.write_export(
                Path(directory),
                "\ufeff猫\tcat\tReading\tExample\n",
                report_for("Alpha.tsv", ["猫"]),
            )
            with self.assertRaisesRegex(ValueError, "byte-order mark"):
                MODULE.validate_export(tsv, report)

    def test_non_nato_filename_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tsv = root / "cards.tsv"
            report = root / "cards.validation.json"
            tsv.write_text("猫\tcat\tReading\tExample\n", encoding="utf-8", newline="\n")
            report.write_text(json.dumps(report_for("cards.tsv", ["猫"])), encoding="utf-8", newline="\n")
            with self.assertRaisesRegex(ValueError, "NATO"):
                MODULE.validate_export(tsv, report)


if __name__ == "__main__":
    unittest.main()
