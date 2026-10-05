import unittest
from datetime import date

from gtd_agent.dates import resolve_deadline


class DeadlineTests(unittest.TestCase):
    def test_requested_shortcuts(self):
        today = date(2026, 9, 23)
        expected = {
            "tomorrow": date(2026, 9, 24),
            "EoW": date(2026, 9, 25),
            "end of week": date(2026, 9, 25),
            "this Friday": date(2026, 9, 25),
            "EoM": date(2026, 9, 30),
            "end of month": date(2026, 9, 30),
            "next week": date(2026, 9, 30),
            "next month": date(2026, 10, 23),
        }
        for phrase, day in expected.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(resolve_deadline(f"Waiting for reply by {phrase}", today).day, day)

    def test_next_month_clamps_last_day(self):
        self.assertEqual(resolve_deadline("by next month", date(2025, 1, 31)).day,
                         date(2025, 2, 28))
        self.assertEqual(resolve_deadline("by next month", date(2024, 1, 31)).day,
                         date(2024, 2, 29))
        self.assertEqual(resolve_deadline("by next month", date(2026, 12, 31)).day,
                         date(2027, 1, 31))

    def test_due_marker_prevents_start_date_becoming_project_deadline(self):
        today = date(2026, 9, 23)
        self.assertIsNone(resolve_deadline("Start studying next week", today,
                                           require_due_marker=True))
        self.assertEqual(resolve_deadline("Finish project by next week", today,
                                          require_due_marker=True).day, date(2026, 9, 30))

    def test_multiple_shortcuts_need_review(self):
        with self.assertRaisesRegex(ValueError, "Several deadline"):
            resolve_deadline("tomorrow or EoW", date(2026, 9, 23))


if __name__ == "__main__":
    unittest.main()
