"""Starting, stopping and restarting the agent when it runs hidden (no window to close)."""
import io
import os
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from helpers import VaultCase
from gtd_agent.cli import main
from gtd_agent.core import process_lock
from gtd_agent.runner import Control, Operation, Runner


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class RequestTests(VaultCase):
    def runner(self, control, calls):
        clock = FakeTime()
        work = Operation("Inbox scan", lambda: calls.append(1) or [], "scan")
        return Runner(self.agent.health, [work], 3, 3, clock=clock.clock, sleep=clock.sleep, control=control)

    def request(self, name, age):
        path = self.settings.state_dir / f"{name}.request"
        path.write_text("requested\n", encoding="utf-8")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_a_stop_request_ends_the_loop_between_cycles(self):
        calls = []
        control = Control(self.settings.state_dir, started=time.time() - 60)
        path = self.request("stop", age=5)
        self.assertEqual(self.runner(control, calls).run(cycles=5), "stop")
        self.assertEqual(calls, [])
        self.assertFalse(path.exists())

    def test_a_restart_request_ends_the_loop_asking_for_a_fresh_copy(self):
        control = Control(self.settings.state_dir, started=time.time() - 60)
        self.request("restart", age=5)
        self.assertEqual(self.runner(control, []).run(cycles=5), "restart")

    def test_a_request_left_from_before_this_copy_started_is_ignored(self):
        calls = []
        control = Control(self.settings.state_dir, started=time.time())
        path = self.request("stop", age=3600)
        self.assertEqual(self.runner(control, calls).run(cycles=2), "done")
        self.assertEqual(calls, [1, 1])
        self.assertFalse(path.exists())


class CommandTests(VaultCase):
    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--config", str(self.settings.config_path), *args])
        return code, out.getvalue()

    def future_request(self, name):
        path = self.settings.state_dir / f"{name}.request"
        path.write_text("requested\n", encoding="utf-8")
        stamp = time.time() + 30
        os.utime(path, (stamp, stamp))

    def log(self):
        return (self.settings.state_dir / "logs" / "agent.log").read_text(encoding="utf-8")

    def test_run_obeys_a_stop_request_and_frees_the_lock(self):
        self.future_request("stop")
        code, _ = self.cli("run")
        self.assertEqual(code, 0)
        self.assertIn("GTD agent started", self.log())
        self.assertIn("Stop requested", self.log())
        with process_lock(self.settings.state_dir, "runner.lock"):
            pass  # free again

    def test_run_restarts_itself_hidden_after_a_restart_request(self):
        self.future_request("restart")
        with mock.patch("gtd_agent.cli.subprocess.Popen") as spawn:
            code, _ = self.cli("run")
        self.assertEqual(code, 0)
        command = spawn.call_args[0][0]
        self.assertTrue(command[1].endswith("run.py"), command)
        self.assertEqual(command[2:], ["--config", str(self.settings.config_path), "run"])
        self.assertIn("Restarting", self.log())

    def test_a_second_copy_says_the_agent_is_already_running(self):
        with process_lock(self.settings.state_dir, "runner.lock"):
            code, out = self.cli("run")
        self.assertEqual(code, 1)
        self.assertIn("already running", self.log())

    def test_a_hidden_agent_that_cannot_start_says_why_in_its_log(self):
        broken = self.base / "broken" / "config.toml"
        broken.parent.mkdir()
        broken.write_text("[agent]\nmode = \"approval\"\n", encoding="utf-8")  # no [vault] root
        code = main(["--config", str(broken), "run"])
        self.assertEqual(code, 1)
        log = (broken.parent / "state" / "logs" / "agent.log").read_text(encoding="utf-8")
        self.assertIn("The agent could not start: config.toml needs [vault] root", log)

    def test_stop_with_no_agent_running_says_so_and_leaves_no_request(self):
        code, out = self.cli("stop")
        self.assertEqual(code, 0)
        self.assertIn("No agent is running", out)
        self.assertFalse((self.settings.state_dir / "stop.request").exists())

    def test_restart_with_no_agent_running_starts_a_hidden_copy(self):
        with mock.patch("gtd_agent.cli.subprocess.Popen") as spawn:
            code, out = self.cli("restart")
        self.assertEqual(code, 0)
        self.assertIn("Started the agent", out)
        self.assertEqual(spawn.call_args[0][0][-3:], ["--config", str(self.settings.config_path), "run"])


if __name__ == "__main__":
    unittest.main()
