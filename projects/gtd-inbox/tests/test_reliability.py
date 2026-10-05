"""Reliability: the run loop never stops silently, and one failing step never blocks the rest."""
import http.client
import io
import json
import os
import time
import unittest
import urllib.error
from datetime import datetime
from unittest import mock

from helpers import VaultCase
from gtd_agent.housekeeping import Housekeeper
from gtd_agent.providers import OpenRouter, ProviderError
from gtd_agent.runner import Operation, Runner, loop_operations


class FakeTime:
    """Clock and sleep for the loop: sleeping moves the clock forward, nothing actually waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def failing_first(real, errors):
    """An operation whose first calls raise the given errors; after that it runs for real."""
    state = {"calls": 0}

    def run():
        state["calls"] += 1
        if state["calls"] <= len(errors):
            raise errors[state["calls"] - 1]
        return real()
    run.state = state
    return run


class RunLoopTests(VaultCase):
    def runner(self, operations):
        time = FakeTime()
        return Runner(self.agent.health, operations, poll_seconds=3, scan_seconds=3, clock=time.clock,
                      sleep=time.sleep)

    def log_text(self) -> str:
        return (self.base / "state" / "logs" / "agent.log").read_text(encoding="utf-8")

    def test_key_and_type_errors_in_every_operation_never_stop_the_loop(self):
        operations = loop_operations(self.agent)
        self.assertEqual([op.label for op in operations],
                         ["Approvals", "Inbox scan", "Housekeeping", "Approval refresh"])
        for op in operations:
            op.run = failing_first(op.run, [KeyError("missing_field"), TypeError("unsupported operand")])
        self.runner(operations).run(cycles=3)
        self.assertEqual([op.run.state["calls"] for op in operations], [3, 3, 3, 3])
        self.assertTrue(self.path("_agent/TODAY.md").exists())  # the third cycle ran the real housekeeping
        log = self.log_text()
        self.assertIn("KeyError: 'missing_field'", log)
        self.assertIn("TypeError: unsupported operand", log)
        self.assertEqual(log.count("Traceback (most recent call last)"), 8)
        for label in ("Approvals", "Inbox scan", "Housekeeping", "Approval refresh"):
            self.assertIn(f"{label} failed", log)

    def test_ctrl_c_and_exit_still_stop_the_loop(self):
        def interrupt():
            raise KeyboardInterrupt

        def leave():
            raise SystemExit(0)
        with self.assertRaises(KeyboardInterrupt):
            self.runner([Operation("Approvals", interrupt, "poll")]).run(cycles=3)
        with self.assertRaises(SystemExit):
            self.runner([Operation("Approvals", leave, "poll")]).run(cycles=3)

    def test_a_repeating_error_is_shown_and_traced_once_then_recovery_is_announced(self):
        same = [ValueError("Inbox note is locked")] * 3
        self.runner([Operation("Inbox scan", failing_first(lambda: [], same), "scan")]).run(cycles=4)
        shown = [line for line in self.said if "Inbox scan" in line]
        self.assertEqual(len(shown), 2, shown)
        self.assertIn("CHECK: Inbox scan: ValueError: Inbox note is locked", shown[0])
        self.assertIn("RECOVERED: Inbox scan", shown[1])
        self.assertEqual(self.log_text().count("Traceback (most recent call last)"), 1)

    def test_ledger_keeps_the_last_run_and_the_last_error(self):
        scan = Operation("Inbox scan", failing_first(lambda: [], [TypeError("bad value")]), "scan")
        self.runner([scan]).run(cycles=2)
        health = self.agent.health.snapshot()
        self.assertIsNotNone(health["last_run"])
        self.assertEqual(health["last_error"]["label"], "Inbox scan")
        self.assertIn("TypeError: bad value", health["last_error"]["text"])
        self.assertIsNotNone(health["last_error"]["recovered_at"])

    def test_an_operation_can_ask_to_run_again_before_the_next_scan(self):
        clock = FakeTime()
        ran = {"inbox": [], "upkeep": []}
        waits = [4.0]  # the first run asks to come back in 4 seconds; the next one is content
        operations = [Operation("Inbox scan", lambda: ran["inbox"].append(clock.now) or [], "scan",
                                again_in=lambda: waits.pop(0) if waits else None),
                      Operation("Housekeeping", lambda: ran["upkeep"].append(clock.now) or [], "scan")]
        Runner(self.agent.health, operations, poll_seconds=3, scan_seconds=60, clock=clock.clock,
               sleep=clock.sleep).run(cycles=4)  # cycles at 0, 3, 6 and 9 seconds
        self.assertEqual(ran["inbox"], [0.0, 6.0])
        self.assertEqual(ran["upkeep"], [0.0])


class QuietInboxLoopTests(VaultCase):
    inbox_quiet = 30

    def test_the_loop_reads_a_new_line_as_soon_as_the_inbox_is_quiet(self):
        self.add_inbox("Buy stamps #errands")
        clock = FakeTime()
        runner = Runner(self.agent.health, loop_operations(self.agent), poll_seconds=3, scan_seconds=600,
                        clock=clock.clock, sleep=clock.sleep)
        self.inbox_untouched(26)
        runner.cycle()  # 0 s: the line is 4 seconds short of quiet
        self.assertEqual(self.pending("inbox"), [])
        for untouched in (29, 32):  # 3 s: still short. 6 s: quiet, long before the next scan is due
            clock.sleep(3)
            self.inbox_untouched(untouched)
            runner.cycle()
        self.assertEqual(len(self.pending("inbox")), 1)


class TodayHealthTests(VaultCase):
    def test_today_reports_the_last_error_until_it_recovers(self):
        keeper = Housekeeper(self.agent)
        label = "Housekeeping › Inbox files"
        self.agent.health.error(label, TypeError("unhashable type: 'slice'"), at=datetime(2026, 9, 28, 9, 40))
        keeper.digest(self.agent.vault(), now=datetime(2026, 9, 28, 9, 41))
        today = self.read("_agent/TODAY.md")
        self.assertIn("Checked 09:41", today)
        self.assertIn("Last error 09:40 in Housekeeping › Inbox files: TypeError: unhashable type: 'slice'.", today)
        self.assertIn("state/logs/agent.log", today)
        self.agent.health.recovered(label, at=datetime(2026, 9, 28, 9, 42))
        keeper.digest(self.agent.vault(), now=datetime(2026, 9, 28, 9, 43))
        self.assertIn("Recovered 09:42.", self.read("_agent/TODAY.md"))

    def test_check_time_refreshes_every_15_minutes_without_other_changes(self):
        keeper = Housekeeper(self.agent)
        vault = self.agent.vault()
        keeper.digest(vault, now=datetime(2026, 9, 28, 10, 0))
        keeper.digest(vault, now=datetime(2026, 9, 28, 10, 10))
        self.assertIn("Checked 10:00", self.read("_agent/TODAY.md"))
        keeper.digest(vault, now=datetime(2026, 9, 28, 10, 16))
        self.assertIn("Checked 10:16", self.read("_agent/TODAY.md"))


class StepIsolationTests(VaultCase):
    remote = True

    def test_a_failing_housekeeping_step_does_not_block_the_later_ones(self):
        def broken():
            raise TypeError("credential store returned None")
        self.provider.available = broken  # the Inbox-files step asks whether the model is available
        self.write("00_INBOX/notes.md", "# Notes\nCall Sam about the budget.\n")
        stamp = time.time() - 600
        os.utime(self.path("00_INBOX/notes.md"), (stamp, stamp))
        Housekeeper(self.agent).run(now=datetime.now())
        self.assertTrue(self.path("_agent/LINT.md").exists())
        self.assertTrue(self.path("_agent/TODAY.md").exists())
        error = self.agent.health.snapshot()["last_error"]
        self.assertEqual(error["label"], "Housekeeping › Inbox files")
        self.assertIn("TypeError: credential store returned None", error["text"])


    def test_a_file_the_model_answer_breaks_waits_instead_of_retrying_every_scan(self):
        answer = {"doc_type": "notes", "title": "Odd notes", "course": "", "target_key": "", "reference_folder": "",
                  "summary": "Notes.", "facts": {"not": "a list"}, "deadlines": [], "actions": [],
                  "explanation": "Notes."}
        self.provider.documents.append(json.loads(json.dumps(answer)))
        self.write("00_INBOX/odd.md", "# Odd\nSomething happens Oct 14.\n")
        stamp = time.time() - 600
        os.utime(self.path("00_INBOX/odd.md"), (stamp, stamp))
        messages = Housekeeper(self.agent).run(now=datetime.now())
        self.assertTrue(any(m.startswith("CHECK: could not read odd.md") for m in messages), messages)
        Housekeeper(self.agent).run(now=datetime.now())  # the next scan, well within 30 minutes
        self.assertEqual([name for name, _ in self.provider.calls].count("document"), 1)
        log = (self.base / "state" / "logs" / "agent.log").read_text(encoding="utf-8")
        self.assertIn("Traceback (most recent call last)", log)
        self.assertIn("odd.md", log)


class SyncedFolderTests(VaultCase):
    """The vault lives in a sync tool's folder whose path has spaces, as Proton Drive names it."""
    base_subdir = "Sync Folder/sample-account/My files/Notes"

    def test_a_scan_an_approval_and_upkeep_work_there(self):
        self.assertIn("Sync Folder/sample-account/My files", self.settings.root.as_posix())
        self.add_inbox("Buy stamps #errands")
        self.assertEqual(self.agent.scan().errors, [])
        Housekeeper(self.agent).run(now=datetime(2026, 9, 28, 9, 0))
        self.assertIn("Checked 09:00", self.read("_agent/TODAY.md"))
        self.tick()
        outcomes = self.agent.sync_review_requests()
        self.assertTrue(any("Applied" in outcome for outcome in outcomes), outcomes)
        self.assertIn("Buy stamps #errands", self.read("01_GTD/SINGLE_ACTIONS.md"))
        self.assertIsNone(self.agent.health.snapshot()["last_error"])
        self.assertEqual([p.name for p in self.path("").rglob("*.tmp")], [])  # nothing left for the sync tool


class CutOffReply:
    """An HTTP response whose body stops halfway."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        raise http.client.IncompleteRead(b'{"choices": [')


class Reply:
    """An HTTP response with a JSON body."""

    def __init__(self, body) -> None:
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self.body


class ModelTransportTests(VaultCase):
    """Any network failure talking to OpenRouter is a model error, so callers back off instead of crashing."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key-not-real"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.model = OpenRouter(self.settings)

    def urlopen(self, **behaviour):
        return mock.patch("gtd_agent.providers.urllib.request.urlopen", **behaviour)

    def test_a_dropped_connection_is_a_model_error(self):
        with self.urlopen(side_effect=http.client.RemoteDisconnected("Remote end closed connection")):
            with self.assertRaises(ProviderError):
                self.model.split("Buy stamps and call Sam")

    def test_a_cut_off_reply_is_a_model_error(self):
        with self.urlopen(return_value=CutOffReply()):
            with self.assertRaises(ProviderError):
                self.model.split("Buy stamps and call Sam")

    def test_an_answer_that_cannot_be_read_is_asked_for_once_more(self):
        good = {"choices": [{"message": {"content": json.dumps({"items": ["Buy stamps", "call Sam"], "reason": "two"})}}]}
        with self.urlopen(side_effect=[Reply({"error": {"message": "upstream hiccup"}}), Reply(good)]) as opened:
            self.assertEqual(self.model.split("Buy stamps and call Sam"), ["Buy stamps", "call Sam"])
        self.assertEqual(opened.call_count, 2)

    def test_two_answers_that_cannot_be_read_are_a_model_error(self):
        broken = {"choices": [{"message": {"content": "not json"}}]}
        with self.urlopen(side_effect=[Reply({"error": {}}), Reply(broken), Reply(broken)]) as opened:
            with self.assertRaisesRegex(ProviderError, "invalid response"):
                self.model.split("Buy stamps and call Sam")
        self.assertEqual(opened.call_count, 2)

    def test_an_answer_cut_off_at_its_length_limit_is_not_asked_for_again(self):
        cut = {"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}
        with self.urlopen(side_effect=[Reply(cut), Reply(cut)]) as opened:
            with self.assertRaisesRegex(ProviderError, "response budget"):
                self.model.split("Buy stamps and call Sam")
        self.assertEqual(opened.call_count, 1)

    def test_an_http_error_names_its_status(self):
        refusal = urllib.error.HTTPError(OpenRouter.__module__, 402, "Payment Required", {}, io.BytesIO(b"{}"))
        with self.urlopen(side_effect=refusal):
            with self.assertRaisesRegex(ProviderError, "HTTP 402"):
                self.model.split("Buy stamps and call Sam")


if __name__ == "__main__":
    unittest.main()
