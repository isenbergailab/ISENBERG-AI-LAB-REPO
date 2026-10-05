import unittest
from datetime import date

import helpers  # noqa: F401  (sets sys.path)
from gtd_agent.markdown import iter_links, parse_frontmatter, section_bounds
from gtd_agent.ops import OpError, apply_op, describe
from gtd_agent.tasks import (actionable_rule, clean_text, format_task, parse_task_line, parse_tasks, with_date,
                             with_status, with_tag, without_tag)

TODAY = date(2026, 9, 28)


class TaskParsingTests(unittest.TestCase):
    def test_fields_tags_links_and_block(self):
        task = parse_task_line("- [ ] Email Jordan [[AREA_X|X]] #computer #15m 🛫 2026-10-01 📅 2026-10-03 "
                               "➕ 2026-09-28 ^gtd-abc", "a.md", 3)
        self.assertEqual(task.dates["due"], date(2026, 10, 3))
        self.assertEqual(task.start, date(2026, 10, 1))
        self.assertEqual(task.created, date(2026, 9, 28))
        self.assertEqual(task.block_id, "gtd-abc")
        self.assertEqual(task.tags, ["#computer", "#15m"])
        self.assertEqual(task.links, ["AREA_X"])
        self.assertEqual(task.context, "#computer")
        self.assertEqual(task.minutes, 15)
        self.assertEqual(task.title, "Email Jordan X")
        self.assertTrue(task.metadata_ok)

    def test_tags_after_dates_and_recurrence(self):
        task = parse_task_line("- [ ] Review 🔁 every week on Sunday 📅 2026-10-04 #anywhere #next")
        self.assertEqual(task.recurrence, "every week on Sunday")
        self.assertTrue(task.is_next)
        self.assertEqual(task.due, date(2026, 10, 4))

    def test_text_after_a_date_is_flagged(self):
        task = parse_task_line("- [ ] Pay rent 📅 2026-10-01 before noon")
        self.assertFalse(task.metadata_ok)
        self.assertIsNone(task.due)

    def test_waiting_parts_and_statuses(self):
        task = parse_task_line("- [/] #waiting Dana Smith: lease copy [[AREA_ADMIN]] 📅 2026-10-05")
        self.assertTrue(task.is_open and task.is_waiting)
        self.assertEqual(task.waiting_parts(), ("Dana Smith", "lease copy"))
        self.assertTrue(parse_task_line("- [x] Done thing").is_done)
        self.assertTrue(parse_task_line("- [-] Dropped").is_cancelled)
        self.assertIsNone(parse_task_line("- plain bullet"))

    def test_code_blocks_and_frontmatter_are_ignored(self):
        text = "---\nkey: X\n---\n## Steps\n```\n- [ ] not a task\n```\n- [ ] real #next\n"
        tasks = parse_tasks(text, "x.md")
        self.assertEqual([t.title for t in tasks], ["real"])
        self.assertEqual(tasks[0].section, "Steps")

    def test_edits_keep_metadata_order(self):
        line = "- [ ] Call Sam #calls 📅 2026-10-01 ^gtd-1"
        self.assertEqual(with_status(line, "x", TODAY), "- [x] Call Sam #calls 📅 2026-10-01 ✅ 2026-09-28 ^gtd-1")
        self.assertEqual(with_tag(line, "#next"), "- [ ] Call Sam #next #calls 📅 2026-10-01 ^gtd-1")
        self.assertEqual(with_date(line, "due", date(2026, 10, 8)), "- [ ] Call Sam #calls 📅 2026-10-08 ^gtd-1")
        self.assertEqual(with_date("- [ ] Call Sam", "due", date(2026, 10, 8)), "- [ ] Call Sam 📅 2026-10-08")
        self.assertEqual(with_tag("- [ ] Call Sam #next", "#next"), "- [ ] Call Sam #next")

    def test_removing_a_tag_touches_nothing_else(self):
        self.assertEqual(without_tag("- [ ] Call Sam #next #calls 📅 2026-10-01 ^gtd-1", "#next"),
                         "- [ ] Call Sam #calls 📅 2026-10-01 ^gtd-1")
        self.assertEqual(without_tag("- [ ] #next Plan it", "#next"), "- [ ] Plan it")
        self.assertEqual(without_tag("- [ ] Read #nextjs docs #Next", "#next"), "- [ ] Read #nextjs docs")
        self.assertEqual(without_tag("- [ ] No tag here #calls", "#next"), "- [ ] No tag here #calls")

    def test_format_and_clean(self):
        line = format_task("Email Jordan", ["#computer", "#next"], dates={"due": date(2026, 10, 1),
                                                                        "created": TODAY}, block_id="gtd-x")
        self.assertEqual(line, "- [ ] Email Jordan #computer #next 📅 2026-10-01 ➕ 2026-09-28 ^gtd-x")
        for bad in ("", "a\n<!-- x -->", "Pay 📅 2026-01-01", "x ^block"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clean_text(bad)

    def test_actionable_rule(self):
        single = "01_GTD/SINGLE_ACTIONS.md"
        self.assertTrue(actionable_rule(parse_task_line("- [ ] A #computer", single), None, TODAY, single))
        self.assertFalse(actionable_rule(parse_task_line("- [ ] A #computer", "p.md"), "active", TODAY, single))
        self.assertTrue(actionable_rule(parse_task_line("- [ ] A #next", "p.md"), "active", TODAY, single))
        self.assertFalse(actionable_rule(parse_task_line("- [ ] A #next", "p.md"), "someday", TODAY, single))
        self.assertFalse(actionable_rule(parse_task_line("- [ ] A #next 🛫 2026-10-13", "p.md"), "active", TODAY, single))
        self.assertFalse(actionable_rule(parse_task_line("- [ ] #waiting A: b #next", "p.md"), "active", TODAY, single))


class MarkdownTests(unittest.TestCase):
    def test_frontmatter(self):
        data = parse_frontmatter('---\nkey: X\narea: "[[AREA_Y]]"\naliases: [A, "B, C"]\nlist:\n  - one\n  - two\n'
                                 'private: true\ndue:\n---\nbody')
        self.assertEqual(data["area"], "[[AREA_Y]]")
        self.assertEqual(data["aliases"], ["A", "B, C"])
        self.assertEqual(data["list"], ["one", "two"])
        self.assertIs(data["private"], True)
        self.assertIsNone(data["due"])

    def test_links_skip_code_and_handle_table_pipes(self):
        text = "| [[NOTE_A\\|Alias]] |\n`[[NOT]]`\n```\n[[ALSO_NOT]]\n```\n![[img.png]] [[B#Head]]"
        links = [(l.target, l.alias, l.heading, l.embed) for l in iter_links(text)]
        self.assertEqual(links, [("NOTE_A", "Alias", "", False), ("img.png", "", "", True), ("B", "", "Head", False)])

    def test_section_bounds(self):
        lines = ["# T", "## Steps", "a", "### Done", "b", "## Log", "c"]
        self.assertEqual(section_bounds(lines, "Steps"), (1, 5))
        self.assertEqual(section_bounds(lines, "Log"), (5, 7))
        self.assertIsNone(section_bounds(lines, "Missing"))


class OpsTests(unittest.TestCase):
    PROJECT = ("---\nkey: P\n---\n# P\n\n## Steps\n- [x] Old ✅ 2026-09-01\n- [ ] First #computer #next\n"
               "- [ ] Second #calls\n\n### Done\n- [x] Earlier\n\n## Log\n- 2026-09-01 start\n")

    def test_new_next_action_goes_after_the_last_next_step(self):
        text = apply_op(self.PROJECT, {"op": "add_task", "path": "p", "section": "Steps", "position": "steps",
                                       "line": "- [ ] New #computer #next ^gtd-1", "marker": "^gtd-1"}, TODAY)
        lines = text.split("\n")
        self.assertEqual(lines.index("- [ ] New #computer #next ^gtd-1"), lines.index("- [ ] First #computer #next") + 1)
        again = apply_op(text, {"op": "add_task", "path": "p", "section": "Steps", "position": "steps",
                                "line": "- [ ] New #computer #next ^gtd-1", "marker": "^gtd-1"}, TODAY)
        self.assertEqual(again, text)

    def test_placeholder_and_missing_section(self):
        text = "# P\n\n## Steps\n_No steps yet._\n\n## Log\n"
        out = apply_op(text, {"op": "add_task", "path": "p", "section": "Steps", "position": "steps",
                              "line": "- [ ] Go"}, TODAY)
        self.assertIn("## Steps\n- [ ] Go\n", out)
        out = apply_op("# P\n\n## Steps\n- [ ] a\n\n## Log\n", {"op": "add_task", "path": "p", "section": "Waiting For",
                                                             "line": "- [ ] #waiting A: b"}, TODAY)
        self.assertLess(out.index("## Waiting For"), out.index("## Log"))
        self.assertIn("## Waiting For\n- [ ] #waiting A: b\n\n## Log", out)

    def test_log_is_newest_first_and_idempotent(self):
        op = {"op": "append_log", "path": "p", "entry": "2026-09-28 Spoke with Sam"}
        out = apply_op(self.PROJECT, op, TODAY)
        self.assertIn("## Log\n- 2026-09-28 Spoke with Sam\n- 2026-09-01 start", out)
        self.assertEqual(apply_op(out, op, TODAY), out)

    def test_complete_set_date_and_errors(self):
        op = {"op": "complete_task", "path": "p", "match": "- [ ] Second #calls", "done": "2026-09-28"}
        out = apply_op(self.PROJECT, op, TODAY)
        self.assertIn("- [x] Second #calls ✅ 2026-09-28", out)
        self.assertEqual(apply_op(out, op, TODAY), out)
        with self.assertRaises(OpError):
            apply_op(self.PROJECT, {"op": "complete_task", "path": "p", "match": "- [ ] Gone"}, TODAY)
        dated = apply_op(self.PROJECT, {"op": "set_date", "path": "p", "match": "- [ ] Second #calls",
                                        "field": "due", "value": "2026-10-05"}, TODAY)
        self.assertIn("- [ ] Second #calls 📅 2026-10-05", dated)
        self.assertIn("+ - [x] Second #calls ✅ 2026-09-28", describe(op, TODAY))

    def test_remove_tag_op_is_idempotent(self):
        op = {"op": "remove_tag", "path": "p", "match": "- [ ] First #computer #next", "tag": "#next"}
        out = apply_op(self.PROJECT, op, TODAY)
        self.assertIn("- [ ] First #computer\n", out)
        self.assertEqual(apply_op(out, op, TODAY), out)
        self.assertIn("+ - [ ] First #computer", describe(op, TODAY))

    def test_replace_link_remove_line_and_create(self):
        text = "See [[Old Name]] and [[Old Name|shown]]\n- [ ] Dup\n- [ ] Dup\n"
        out = apply_op(text, {"op": "replace_link", "path": "p", "old": "Old Name", "new": "NEW"}, TODAY)
        self.assertIn("[[NEW|Old Name]] and [[NEW|shown]]", out)
        out = apply_op(text, {"op": "remove_line", "path": "p", "match": "- [ ] Dup", "occurrence": 2}, TODAY)
        self.assertEqual(out.count("- [ ] Dup"), 1)
        self.assertEqual(apply_op("", {"op": "create_file", "path": "p", "text": "# New"}, TODAY), "# New\n")
        with self.assertRaises(OpError):
            apply_op("exists", {"op": "create_file", "path": "p", "text": "# New"}, TODAY)


if __name__ == "__main__":
    unittest.main()
