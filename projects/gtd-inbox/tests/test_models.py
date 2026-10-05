"""Model calls: privacy fields on every request, the monthly budget, and per-job models and endpoints."""
import io
import json
import os
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest import mock

from helpers import TODAY, VaultCase
from gtd_agent.cli import main
from gtd_agent.housekeeping import Housekeeper
from gtd_agent.providers import BudgetPaused, OpenRouter

PROJECTS = [{"key": "DEMO_PROJECT", "name": "Demo Project", "aliases": [], "status": "active", "area": "AREA_CLUB"}]
AREAS = [{"key": "AREA_CLUB", "aliases": ["Club"]}]
CHOICE = {"type": "choice", "choice": "single", "confidence": 0.9, "probabilities": {"single": 0.9}}


class Reply:
    """One HTTP reply from a model endpoint."""

    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self.body


def chat_reply(content, cost=0.001):
    return {"choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": cost}}


def decision_reply(cost=0.00002):
    answers = {"operation": dict(CHOICE, choice="new_capture", probabilities={"new_capture": 0.9}),
               "category": dict(CHOICE, choice="next_action", probabilities={"next_action": 0.9}),
               "multiplicity": CHOICE}
    return {"answers": answers, "usage": {"input_tokens": 400, "output_tokens": 30, "cost": cost}}


class ModelCase(VaultCase):
    remote = True

    def setUp(self):
        super().setUp()
        keys = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key-not-real", "GTD_SECRET_ROUTER": "router-key"})
        keys.start()
        self.addCleanup(keys.stop)
        self.sent = []
        self.replies = []
        network = mock.patch("gtd_agent.providers.urllib.request.urlopen", side_effect=self.answer)
        network.start()
        self.addCleanup(network.stop)
        self.model = OpenRouter(self.settings, budget=self.agent.budget)

    def answer(self, request, timeout=None):
        self.sent.append({"url": request.full_url, "body": json.loads(request.data.decode("utf-8")),
                          "key": request.get_header("Authorization")})
        return Reply(self.replies.pop(0))

    def call_every_job(self):
        self.replies += [decision_reply(), chat_reply({"items": ["Buy stamps"], "reason": "one"}),
                         chat_reply({"route": "next_action"}),
                         chat_reply({"doc_type": "notes"}), chat_reply({"decision": "x"}),
                         chat_reply({"flags": []}), chat_reply({"groups": []})]
        self.model.decide("Buy stamps")
        self.model.split("Buy stamps")
        self.model.interpret("Buy stamps", "issue", PROJECTS, AREAS, [], [])
        self.model.document("# Notes\n", "notes.md", TODAY, PROJECTS, AREAS, [])
        self.model.decided("Decided: yes", "DEMO_PROJECT", [], [], TODAY)
        self.model.vague_check([{"id": "a1", "text": "Handle taxes"}])
        self.model.someday_topics([{"id": "s1", "text": "Sail"}])


class PrivacyFieldTests(ModelCase):
    def test_every_openrouter_request_asks_for_no_training_and_no_retention(self):
        self.call_every_job()
        self.assertEqual(len(self.sent), 7)
        for request in self.sent:
            with self.subTest(url=request["url"], model=request["body"]["model"]):
                self.assertEqual(request["body"]["provider"]["data_collection"], "deny")
                self.assertIs(request["body"]["provider"]["zdr"], True)


class BudgetTests(ModelCase):
    def test_each_call_cost_is_recorded_for_the_month(self):
        self.replies += [chat_reply({"items": ["a"], "reason": ""}, cost=0.25),
                         chat_reply({"items": ["b"], "reason": ""}, cost=0.5)]
        self.model.split("a")
        self.model.split("b")
        self.assertAlmostEqual(self.agent.budget.spent(), 0.75)

    def test_at_80_percent_today_warns(self):
        self.agent.budget.record("documents", "z-ai/glm-5.3", {"cost": 4.0})
        Housekeeper(self.agent).digest(self.agent.vault(), now=datetime(2026, 9, 28, 10, 0))
        self.assertIn("Model budget: $4.00 of $5.00 used this month (80%).", self.read("_agent/TODAY.md"))

    def test_at_the_cap_only_inbox_lines_still_reach_a_model(self):
        self.agent.budget.record("documents", "z-ai/glm-5.3", {"cost": 5.0})
        for paused in (lambda: self.model.document("# Notes\n", "notes.md", TODAY, PROJECTS, AREAS, []),
                       lambda: self.model.decided("Decided: yes", "DEMO_PROJECT", [], [], TODAY),
                       lambda: self.model.vague_check([{"id": "a1", "text": "Handle taxes"}]),
                       lambda: self.model.someday_topics([{"id": "s1", "text": "Sail"}])):
            with self.assertRaises(BudgetPaused):
                paused()
        self.assertEqual(self.sent, [])
        self.replies += [decision_reply(), chat_reply({"items": ["Buy stamps"], "reason": "one"}),
                         chat_reply({"route": "next_action"})]
        self.model.decide("Buy stamps")
        self.model.split("Buy stamps")
        self.model.interpret("Buy stamps", "issue", PROJECTS, AREAS, [], [])
        self.assertEqual(len(self.sent), 3)
        Housekeeper(self.agent).digest(self.agent.vault(), now=datetime(2026, 9, 28, 10, 0))
        self.assertIn("Model budget reached: $5.00 of $5.00 this month.", self.read("_agent/TODAY.md"))

    def test_an_inbox_file_waits_for_the_budget_without_a_model_call(self):
        self.agent.budget.record("documents", "z-ai/glm-5.3", {"cost": 5.0})
        self.write("00_INBOX/notes.md", "# Notes\nCall Sam.\n")
        stamp = time.time() - 600
        os.utime(self.path("00_INBOX/notes.md"), (stamp, stamp))
        Housekeeper(self.agent).run(now=datetime.now())
        proposal = json.loads(self.pending("document")[0]["proposal_json"])
        self.assertIn("monthly model budget", proposal["reason"])
        self.assertEqual([name for name, _ in self.provider.calls if name == "document"], [])


class EndpointTests(ModelCase):
    def extra_config(self):
        return ("[models]\ndocuments = \"local/reader\"\n"
                "[endpoints]\ndocuments = \"https://models.example.test/v1/chat/completions\"\n"
                "[endpoint_keys]\ndocuments = \"router\"\n")

    def test_a_job_moves_to_another_endpoint_through_config_alone(self):
        self.replies += [chat_reply({"doc_type": "notes"}), chat_reply({"items": ["a"], "reason": ""})]
        self.model.document("# Notes\n", "notes.md", TODAY, PROJECTS, AREAS, [])
        self.model.split("a")
        moved, stayed = self.sent
        self.assertEqual(moved["url"], "https://models.example.test/v1/chat/completions")
        self.assertEqual(moved["body"]["model"], "local/reader")
        self.assertEqual(moved["key"], "Bearer router-key")
        self.assertNotIn("provider", moved["body"])  # OpenRouter's routing fields mean nothing elsewhere
        self.assertEqual(stayed["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(stayed["body"]["model"], "z-ai/glm-5.3-flash")
        self.assertEqual(stayed["key"], "Bearer test-key-not-real")

    def test_doctor_names_each_moved_job_and_the_budget(self):
        self.agent.budget.record("split", "z-ai/glm-5.3-flash", {"cost": 1.25})
        out = io.StringIO()
        with redirect_stdout(out):
            main(["--config", str(self.settings.config_path), "doctor"])
        text = out.getvalue()
        self.assertIn("documents: local/reader at https://models.example.test/v1/chat/completions "
                      "(not OpenRouter: no privacy fields are sent; trust this endpoint yourself)", text)
        self.assertIn("Model budget: $1.25 of $5.00 used this month", text)


if __name__ == "__main__":
    unittest.main()
