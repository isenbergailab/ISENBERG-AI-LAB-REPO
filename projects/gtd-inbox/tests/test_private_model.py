"""The private model (phase 3 item 6): a model on SampleOwner's own machines may read his private items; a remote one never.

Off by default. Nothing here reaches a real model: a local HTTP server stands in for the private model, and
`FakeProvider` stands in for OpenRouter.
"""
from __future__ import annotations

import dataclasses
import http.client
import io
import json
import os
import tempfile
import textwrap
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from helpers import TODAY, VaultCase, draft, make_settings, make_vault  # noqa: F401  (helpers sets the path to gtd_agent)
from gtd_agent import providers
from gtd_agent.cli import main
from gtd_agent.core import Capture, private_host_ok
from gtd_agent.feedback import with_feedback
from gtd_agent.providers import OpenRouter, PrivateModelDown, PrivateRefused, ProviderError, knock, refuse_private
from gtd_agent.workflow import ITEM_END, PROPOSAL_VERSION, ApprovalError

URL = "http://100.64.1.2:11434/v1/chat/completions"


def on(**values: str) -> str:
    """A [private_model] table that is switched on; each keyword replaces or adds a key (its value as TOML)."""
    table = {"enabled": "true", "url": f'"{URL}"', "model": '"qwen3:8b"', **values}
    return "".join(f"{key} = {value}\n" for key, value in table.items())


class HostRuleTests(unittest.TestCase):
    """Private text may be sent only to this computer, to SampleOwner's tailnet, or to a host he names himself."""

    def test_this_computer_is_allowed(self):
        for url in ("http://localhost:11434/v1/chat/completions", "http://127.0.0.1:11434/v1", "http://127.1.2.3/v1",
                    "http://[::1]:11434/v1", "https://LOCALHOST./v1"):
            with self.subTest(url):
                self.assertTrue(private_host_ok(url))

    def test_the_tailnet_is_allowed(self):
        for url in ("http://100.64.0.1:11434/v1", "http://100.127.255.254/v1", "http://local-model.tail1a2b3.ts.net:11434/v1",
                    "https://[fd7a:115c:a1e0::1]/v1", "http://[fd7a:115c:a1e0:ffff::1]/v1"):
            with self.subTest(url):
                self.assertTrue(private_host_ok(url))

    def test_everything_else_is_refused(self):
        for url in ("https://openrouter.ai/api/v1/chat/completions", "http://192.168.1.20:11434/v1", "http://10.0.0.5/v1",
                    "http://100.128.0.1/v1", "http://100.63.255.255/v1", "http://127.0.0.1.evil.example/v1",
                    "http://evilts.net/v1", "http://[fd7a:115c:a1e1::1]/v1", "http://[fd7a:115d:a1e0::1]/v1",
                    "http://localhost.evil.example/v1", "http://evil.ts.net.example/v1", "http://ts.net/v1",
                    "http://.ts.net/v1", "http://127.0.0.1@evil.example/v1", "http://user:pw@127.0.0.1/v1",
                    "http://evil.example#@127.0.0.1/v1", "ftp://127.0.0.1/v1", "127.0.0.1:11434", "", "http:///v1",
                    "http://[::ffff:8.8.8.8]/v1", "http://0.0.0.0/v1", "http://127.0.0.1:abc/v1",
                    "http://user@127.0.0.1/v1", "http://:pw@127.0.0.1/v1", "http://@127.0.0.1/v1"):
            with self.subTest(url):
                self.assertFalse(private_host_ok(url))

    def test_a_host_you_name_is_allowed_by_its_whole_name_only(self):
        self.assertTrue(private_host_ok("http://local-model:11434/v1", ["local-model"]))
        self.assertTrue(private_host_ok("http://LOCAL-MODEL./v1", ["local-model"]))
        self.assertTrue(private_host_ok("http://local-model/v1", ["LOCAL-MODEL."]))
        self.assertTrue(private_host_ok("http://192.168.1.20:11434/v1", ["192.168.1.20"]))
        self.assertFalse(private_host_ok("http://local-model.evil.example/v1", ["local-model"]))
        self.assertFalse(private_host_ok("http://evillocal-model/v1", ["local-model"]))
        self.assertFalse(private_host_ok("http://local-model:11434/v1", []))
        self.assertFalse(private_host_ok("http:///v1", ["."]))  # a name that is only a dot never matches no host


class SettingsTests(unittest.TestCase):
    def settings(self, extra: str = ""):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        make_vault(base / "vault")
        return make_settings(base, extra=f"[private_model]\n{extra}" if extra else "")

    def test_it_is_off_by_default(self):
        s = self.settings()
        self.assertEqual((s.private_model_enabled, s.private_model_url, s.private_model_name, s.private_model_key,
                          s.private_model_hosts, s.private_model_jobs, s.private_model_timeout),
                         (False, "", "", "", (), ("interpret",), 180))

    def test_the_table_sets_each_value(self):
        s = self.settings(on(key='"local-model"', trusted_hosts='["nas", "192.168.1.9"]', jobs='["interpret"]',
                             timeout_seconds="60"))
        self.assertEqual((s.private_model_enabled, s.private_model_url, s.private_model_name, s.private_model_key,
                          s.private_model_hosts, s.private_model_jobs, s.private_model_timeout),
                         (True, URL, "qwen3:8b", "local-model", ("nas", "192.168.1.9"), ("interpret",), 60))

    def test_what_is_off_is_not_checked(self):
        s = self.settings('url = "https://api.example.org/v1"\n')
        self.assertFalse(s.private_model_enabled)

    def test_the_limits_themselves_are_allowed(self):
        self.assertEqual(self.settings(on(timeout_seconds="5")).private_model_timeout, 5)
        self.assertEqual(self.settings(on(timeout_seconds="1800")).private_model_timeout, 1800)

    def test_a_job_named_twice_counts_once(self):
        self.assertEqual(self.settings(on(jobs='["interpret", "interpret"]')).private_model_jobs, ("interpret",))

    def test_an_address_off_your_machines_cannot_be_turned_on(self):
        for url in ("https://openrouter.ai/api/v1/chat/completions", "http://192.168.1.20:11434/v1",
                    "http://127.0.0.1.evil.example/v1", "http://localhost.evil.example/v1"):
            with self.subTest(url), self.assertRaisesRegex(ValueError, "this computer or your tailnet"):
                self.settings(on(url=f'"{url}"'))

    def test_its_key_must_be_its_own_secret(self):
        for name in ("openrouter", "telegram", "calendar", "radicale", "agent-mail"):
            with self.subTest(name), self.assertRaisesRegex(ValueError, "its own secret"):
                self.settings(on(key=f'"{name}"'))
        with self.assertRaisesRegex(ValueError, "its own secret"):
            self.settings(on(key='"Bad Name"'))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        make_vault(Path(temp.name) / "vault")
        with self.assertRaisesRegex(ValueError, "its own secret"):  # a name another endpoint already uses
            make_settings(Path(temp.name), extra='[endpoint_keys]\ndefault = "router"\n[private_model]\n'
                          + on(key='"router"'))
        self.assertEqual(self.settings(on(key='"local-model"')).private_model_key, "local-model")

    def test_a_key_is_not_checked_while_it_is_off(self):
        self.assertEqual(self.settings('key = "openrouter"\n').private_model_key, "openrouter")

    def test_a_host_you_trust_by_name_can(self):
        s = self.settings(on(url='"http://192.168.1.20:11434/v1"', trusted_hosts='["192.168.1.20"]'))
        self.assertTrue(s.private_model_enabled)

    def test_a_job_is_ready_when_it_is_on_listed_and_at_a_safe_address(self):
        self.assertFalse(self.settings().private_model_ready("interpret"))
        s = self.settings(on())
        self.assertTrue(s.private_model_ready("interpret"))
        self.assertFalse(s.private_model_ready("route"))
        self.assertFalse(self.settings(on(jobs="[]")).private_model_ready("interpret"))
        self.assertFalse(dataclasses.replace(s, private_model_enabled=False).private_model_ready("interpret"))
        elsewhere = dataclasses.replace(s, private_model_url="https://openrouter.ai/api/v1/chat/completions")
        self.assertFalse(elsewhere.private_model_ready("interpret"))
        named = dataclasses.replace(s, private_model_url="http://nas:11434/v1", private_model_hosts=("nas",))
        self.assertTrue(named.private_model_ready("interpret"))

    def test_values_that_cannot_work_are_refused(self):
        for bad in ({"url": '""'}, {"model": '""'}, {"jobs": '["documents"]'}, {"jobs": '"interpret"'}, {"jobs": "[3]"},
                    {"timeout_seconds": "4"}, {"timeout_seconds": "1801"}, {"trusted_hosts": '"nas"'},
                    {"trusted_hosts": '[""]'}, {"trusted_hosts": '["http://nas"]'}, {"trusted_hosts": '["nas/x"]'},
                    {"trusted_hosts": '["a b"]'}, {"trusted_hosts": '["me@nas"]'}, {"trusted_hosts": "[3]"},
                    {"key": '["a"]'}, {"jobs": '""'}, {"trusted_hosts": '""'}):
            with self.subTest(bad), self.assertRaises(ValueError):
                self.settings(on(**bad))

    def test_it_needs_an_address_and_a_model_to_be_on(self):
        for bad in ({"url": '""'}, {"model": '""'}):
            with self.subTest(bad), self.assertRaisesRegex(ValueError, "needs a url and a model"):
                self.settings(on(**bad))

    def test_what_you_type_is_trimmed(self):
        s = self.settings(on(url=f'" {URL} "', model='" qwen3:8b "', key='" local-model "'))
        self.assertEqual((s.private_model_url, s.private_model_name, s.private_model_key),
                         (URL, "qwen3:8b", "local-model"))


def completion(content, finish: str = "stop") -> dict:
    """What a chat endpoint answers with, the model's JSON as the message."""
    return {"choices": [{"message": {"content": json.dumps(content)}, "finish_reason": finish}]}


class LocalModel:
    """The private model's stand-in: an HTTP server on this computer that keeps what it is sent."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict, dict]] = []   # path, headers, JSON body
        self.replies: list[tuple[int, object, dict]] = []  # status, body (JSON or bytes), headers; then 200 and {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                owner.requests.append((self.path, dict(self.headers), json.loads(body) if body else {}))
                status, payload, headers = owner.replies.pop(0) if owner.replies else (200, completion({}), {})
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST  # a redirect is followed with a GET: whatever follows it is seen

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class LaneCase(unittest.TestCase):
    """OpenRouter's private lane against a local server, with the remote lane watched."""
    config = ""   # more keys for [private_model]
    enabled = True

    def setUp(self) -> None:
        self.model = LocalModel()
        self.addCleanup(self.model.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        make_vault(base / "vault")
        table = on(url=f'"{self.model.url}"') if self.enabled else ""
        self.settings = make_settings(base, extra="[private_model]\n" + table + self.config)
        env = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "remote-key", "GTD_SECRET_LOCAL_MODEL": "local-key"})
        env.start()
        self.addCleanup(env.stop)
        remote = mock.patch("gtd_agent.providers._post", side_effect=AssertionError("the remote lane was used"))
        self.remote = remote.start()
        self.addCleanup(remote.stop)
        self.provider = OpenRouter(self.settings)

    def interpret(self, text: str = "Call the clinic about my forms #private", **kw):
        return self.provider.interpret(text, "Private model", [{"key": "TAXES"}], [{"key": "AREA_ADMIN"}], [], [],
                                       private=True, **kw)


class PrivateLaneTests(LaneCase):
    def test_a_private_interpretation_goes_to_the_private_model_and_nowhere_else(self):
        self.model.replies.append((200, completion({"route": "next_action", "title": "Call the clinic"}), {}))
        self.assertEqual(self.interpret(), {"route": "next_action", "title": "Call the clinic"})
        (path, headers, body), = self.model.requests
        self.assertEqual((path, body["model"], body["stream"]), ("/v1/chat/completions", "qwen3:8b", False))
        sent = json.loads(body["messages"][1]["content"])
        self.assertEqual(sent["capture"], "Call the clinic about my forms #private")
        self.assertEqual(sent["projects"], [{"key": "TAXES"}])        # private projects are named: the model is yours
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertIs(body["response_format"]["json_schema"]["strict"], True)
        self.assertEqual(body["max_tokens"], 4096)
        self.assertNotIn("provider", body)                            # OpenRouter's fields go to OpenRouter only
        self.assertNotIn("reasoning", body)
        self.assertNotIn("Authorization", headers)                    # no key unless you name one
        self.remote.assert_not_called()

    def test_a_key_you_name_goes_to_the_private_model_only(self):
        provider = OpenRouter(dataclasses.replace(self.settings, private_model_key="local-model"))
        provider.interpret("x #private", "Private model", [], [], [], [], private=True)
        self.assertEqual(self.model.requests[0][1]["Authorization"], "Bearer local-key")
        self.remote.assert_not_called()

    def test_a_key_that_is_not_saved_is_named_and_nothing_is_sent(self):
        provider = OpenRouter(dataclasses.replace(self.settings, private_model_key="missing"))
        with self.assertRaisesRegex(ProviderError, "set-secret missing"):
            provider.interpret("x #private", "Private model", [], [], [], [], private=True)
        self.assertEqual(self.model.requests, [])

    def test_an_answer_that_cannot_be_read_is_asked_for_once_more(self):
        self.model.replies.append((200, {"choices": [{"message": {"content": "not json"}, "finish_reason": "stop"}]}, {}))
        self.model.replies.append((200, completion({"route": "next_action"}), {}))
        self.assertEqual(self.interpret(), {"route": "next_action"})
        self.assertEqual(len(self.model.requests), 2)

    def test_an_answer_that_cannot_be_read_twice_is_an_error(self):
        bad = (200, {"choices": [{"message": {"content": "not json"}, "finish_reason": "stop"}]}, {})
        self.model.replies += [bad, bad]
        with self.assertRaisesRegex(ProviderError, "The private model returned an invalid response") as caught:
            self.interpret()
        self.assertNotIsInstance(caught.exception, PrivateModelDown)  # asking again later would not help
        self.assertEqual(len(self.model.requests), 2)

    def test_a_reply_that_is_not_an_object_is_an_error(self):
        self.model.replies.append((200, [1, 2], {}))
        with self.assertRaisesRegex(ProviderError, "non-object response"):
            self.interpret()

    def test_a_choice_that_is_not_an_object_is_an_invalid_answer(self):
        self.model.replies += [(200, {"choices": ["oops"]}, {})] * 2
        with self.assertRaisesRegex(ProviderError, "invalid response"):
            self.interpret()

    def test_an_answer_cut_off_by_its_length_is_not_used(self):
        self.model.replies.append((200, completion({"route": "next_action"}, finish="length"), {}))
        with self.assertRaisesRegex(ProviderError, "The private model ran out of response budget"):
            self.interpret()

    def test_a_reply_that_is_not_json_is_the_model_being_down(self):
        self.model.replies.append((200, b"<html>not a model</html>", {}))
        with self.assertRaisesRegex(PrivateModelDown, "JSONDecodeError"):
            self.interpret()

    def test_a_connection_cut_off_in_the_middle_is_the_model_being_down(self):
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=http.client.IncompleteRead(b"par")):
            with self.assertRaisesRegex(PrivateModelDown, "IncompleteRead"):
                self.interpret()

    def test_a_server_error_is_reported_by_its_code_alone(self):
        self.model.replies.append((500, b"internal details about the clinic", {}))
        with self.assertRaises(PrivateModelDown) as caught:
            self.interpret()
        self.assertEqual(str(caught.exception), "The private model request failed: HTTP 500")

    def test_a_redirect_is_not_followed(self):
        elsewhere = LocalModel()
        self.addCleanup(elsewhere.stop)
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code):
                self.model.replies.append((code, b"", {"Location": elsewhere.url}))
                with self.assertRaisesRegex(ProviderError, f"HTTP {code}"):
                    self.interpret()
        self.assertEqual(elsewhere.requests, [])

    def test_a_proxy_in_the_environment_is_not_used(self):
        proxy = {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9", "NO_PROXY": "", "no_proxy": ""}
        with mock.patch.dict(os.environ, proxy):
            self.interpret()
        self.assertEqual(len(self.model.requests), 1)

    def test_no_money_is_counted_for_a_model_that_is_yours(self):
        budget = mock.Mock()
        provider = OpenRouter(self.settings, budget=budget)
        provider.interpret("x #private", "Private model", [], [], [], [], private=True)
        budget.allows.assert_not_called()
        budget.record.assert_not_called()

    def test_a_server_that_does_not_answer_is_given_up_on_in_seconds(self):
        with mock.patch("gtd_agent.providers.socket.create_connection", side_effect=TimeoutError) as connect:
            with self.assertRaisesRegex(ProviderError, "The private model request failed: TimeoutError"):
                self.interpret()
        self.assertLessEqual(connect.call_args.kwargs["timeout"], 10)  # not the 180 seconds a reply may take
        self.assertEqual(self.model.requests, [])

    def test_a_dead_server_is_an_error_not_a_hang(self):
        self.model.stop()
        with self.assertRaisesRegex(PrivateModelDown, "The private model request failed"):
            self.interpret()


class PrivateLaneTimeoutTests(LaneCase):
    config = "timeout_seconds = 60\n"

    def test_the_reply_time_you_set_is_the_one_waited(self):
        with mock.patch("gtd_agent.providers._post_private", wraps=providers._post_private) as post:
            self.interpret()
        self.assertEqual(post.call_args.args[3], 60)


class KnockTests(unittest.TestCase):
    def test_a_url_without_a_port_is_knocked_at_the_scheme_default(self):
        for url, port in (("http://127.0.0.1/v1", 80), ("https://127.0.0.1/v1", 443), ("http://127.0.0.1:11434/v1", 11434)):
            with self.subTest(url), mock.patch("gtd_agent.providers.socket.create_connection") as connect:
                knock(url)
            self.assertEqual(connect.call_args.args, (("127.0.0.1", port),))

    def test_it_waits_no_longer_than_the_reply_time_allows(self):
        with mock.patch("gtd_agent.providers.socket.create_connection") as connect:
            knock(URL, timeout=2)
        self.assertEqual(connect.call_args.kwargs["timeout"], 2)


class PrivateLaneOffTests(LaneCase):
    enabled = False

    def test_off_means_private_text_goes_to_no_model(self):
        with self.assertRaisesRegex(PrivateRefused, "off for this job"):
            self.interpret()
        self.assertEqual(self.model.requests, [])
        self.remote.assert_not_called()


class PrivateLaneJobsTests(LaneCase):
    config = "jobs = []\n"

    def test_a_job_that_is_not_listed_is_refused(self):
        with self.assertRaisesRegex(PrivateRefused, "off for this job"):
            self.interpret()
        self.assertEqual(self.model.requests, [])


class PrivateLaneAddressTests(LaneCase):
    def test_the_address_is_checked_again_when_the_text_is_about_to_leave(self):
        for url in ("https://openrouter.ai/api/v1/chat/completions", "http://192.168.1.20:11434/v1"):
            provider = OpenRouter(dataclasses.replace(self.settings, private_model_url=url))
            with self.subTest(url), self.assertRaisesRegex(PrivateRefused, "not this computer or your tailnet"):
                provider.interpret("x #private", "Private model", [], [], [], [], private=True)
        self.assertEqual(self.model.requests, [])
        self.remote.assert_not_called()


class TripwireTests(unittest.TestCase):
    def test_it_looks_into_every_kind_of_container(self):
        for request in ("a #private b", ["x", ["#private"]], ("x", "#private"), {"a": {"b": ["#private"]}},
                        [{"k": ("#private",)}]):
            with self.subTest(request), self.assertRaises(PrivateRefused):
                refuse_private(request)

    def test_it_finds_the_tag_in_any_case_and_beside_punctuation(self):
        for text in ("#PRIVATE", "(#private)", "call #private.", "a,#private,b", "x #Private"):
            with self.subTest(text), self.assertRaises(PrivateRefused):
                refuse_private({"capture": text})

    def test_it_lets_other_text_and_other_values_through(self):
        refuse_private({"capture": "a #privately written #private_note", "n": 3, "none": None, "flag": True,
                        "list": [1, 2.5, "private"]})


class RemoteTripwireTests(LaneCase):
    """The last gate before a remote model: text tagged #private never leaves, whatever the caller meant."""

    def test_a_remote_interpretation_with_a_private_capture_is_refused_unsent(self):
        with self.assertRaisesRegex(PrivateRefused, "#private"):
            self.provider.interpret("Call the clinic #private", "Jev", [], [], [], [])
        self.remote.assert_not_called()

    def test_the_tag_is_found_anywhere_in_what_would_be_sent(self):
        for where in ({"items": [{"id": "a", "title": "Pay rent #private"}]}, {"projects": [{"name": "x #Private"}]},
                      {"teacher_feedback": ["keep it (#private)"]}):
            with self.subTest(where), self.assertRaises(PrivateRefused):
                self.provider._chat("system", {"capture": "ok", **where}, "n", {}, job="interpret")
        self.remote.assert_not_called()

    def test_routing_is_refused_the_same_way(self):
        with self.assertRaises(PrivateRefused):
            self.provider.decide("Call the clinic #private")
        self.remote.assert_not_called()

    def test_text_without_the_tag_goes_through(self):
        self.remote.side_effect = None
        self.remote.return_value = completion({"route": "next_action"})
        self.provider.interpret("Call the dentist #privately", "Jev", [], [], [], [])
        self.remote.assert_called_once()


MOVE = textwrap.dedent("""\
    ---
    key: MOVE
    type: project
    status: active
    area:
    due:
    priority:
    aliases: [Move, The move]
    review:
    private: true
    ---
    # MOVE

    ## Steps
    - [ ] Ask the landlord about the deposit #calls #next

    ## Log
    """)
COMPLETE = lambda text, projects, areas, items: draft(  # noqa: E731 - the model picks the landlord task it was offered
    route="completion_report", action_id=next(i["id"] for i in items if "landlord" in i["title"]),
    completion_evidence="Asked the landlord")


def down(*args):
    raise PrivateModelDown("The private model request failed: ConnectionRefusedError")


class PrivateModelCase(VaultCase):
    """A vault with a private project, and the private model switched on."""

    def extra_files(self):
        return {"02_PROJECTS/MOVE/MOVE.md": MOVE}

    def extra_config(self):
        return "[private_model]\n" + on()

    def scan_one(self, line, answer):
        self.add_inbox(line)
        self.provider.interpretations.append(answer)
        (item,) = self.agent.scan().new_proposals
        return item["proposal"]

    def approve(self, line, answer):
        proposal = self.scan_one(line, answer)
        self.tick()
        self.agent.sync_review_requests()
        return proposal


class PrivateCaptureTests(PrivateModelCase):
    """A private capture is read by the private model when it is on, and by no other model. Remote inference
    on or off changes nothing here, so the remote subclass runs every test again."""

    def test_a_private_capture_is_read_by_the_private_model_alone(self):
        proposal = self.scan_one("Book the movers #private", draft(title="Book the movers", context="#calls"))
        self.assertEqual([name for name, _ in self.provider.calls], ["interpret"])  # no routing, no split
        self.assertIs(self.provider.calls[0][1]["private"], True)
        self.assertEqual((proposal["kind"], proposal["private"]), ("next_action", True))
        self.assertIn("private model", proposal["reason"].lower())

    def test_it_is_offered_the_private_projects_and_tasks(self):
        self.scan_one("Did I ask the landlord yet #private", draft(title="Ask again", context="#calls"))
        offered = self.provider.calls[0][1]
        self.assertEqual({"MOVE", "DEMO_PROJECT"} - {p["key"] for p in offered["projects"]}, set())
        self.assertIn("Ask the landlord about the deposit", [i["title"] for i in offered["items"]])

    def test_an_approved_private_item_is_filed_as_the_private_model_said_and_teaches_nothing(self):
        self.scan_one("Book the movers #private", draft(title="Book the movers", context="#calls", project_key="MOVE",
                                                        lesson="Movers are booked by phone."))
        self.assertNotIn("Lesson saved on approval", self.read("_agent/APPROVAL.md"))
        self.tick()
        self.agent.sync_review_requests()
        self.assertRegex(self.read("02_PROJECTS/MOVE/MOVE.md"), r"- \[ \] Book the movers .*#private")
        self.assertEqual(list(self.agent.ledger.list_learning()), [])
        self.assertEqual(self.agent.ledger.learning_examples("Book the movers by phone"), [])

    def test_a_private_task_can_be_completed(self):
        self.scan_one("Asked the landlord about the deposit #private", COMPLETE)
        self.tick()
        self.agent.sync_review_requests()
        self.assertIn("- [x] Ask the landlord about the deposit #calls #next ✅ 2026-09-28",
                      self.read("02_PROJECTS/MOVE/MOVE.md"))

    def test_it_is_shown_no_lessons_from_other_items(self):
        self.agent.ledger.import_learning("old", {"capture": "Book the movers for the move",
                                                  "lesson": "Movers are booked by phone."}, "2026-09-01T00:00:00")
        self.scan_one("Book the movers #private", draft(title="Book the movers", context="#calls"))
        self.assertEqual(self.provider.calls[0][1]["examples"], [])

    def test_a_stale_private_proposal_without_a_reading_waits_while_the_model_is_off(self):
        self.scan_one("Book the movers #private", down)
        (row,) = self.pending("inbox")
        self.agent.ledger.set_status(row["id"], "stale")
        self.agent.settings = dataclasses.replace(self.agent.settings, private_model_enabled=False)
        rebuilt = self.agent.scan().new_proposals[0]["proposal"]
        self.assertEqual((rebuilt["kind"], rebuilt["private"]), ("manual", True))
        self.assertIn("went stale", rebuilt["reason"])

    def test_a_proposal_that_was_private_stays_private_when_its_project_no_longer_is(self):
        self.scan_one("Book the movers [[MOVE]]", draft(title="Book the movers", context="#calls", project_key="MOVE"))
        (row,) = self.pending("inbox")
        self.write("02_PROJECTS/MOVE/MOVE.md", self.read("02_PROJECTS/MOVE/MOVE.md").replace("private: true", "private: false"))
        self.agent.ledger.set_status(row["id"], "stale")
        rebuilt = self.agent.scan().new_proposals[0]["proposal"]
        self.assertIs(rebuilt.get("private"), True)

    def test_the_private_model_is_told_it_is_reading_a_private_item(self):
        self.scan_one("Book the movers #private", draft(title="Book the movers", context="#calls"))
        self.assertEqual(self.provider.calls[0][1]["issue"], "Private item, read by the private model")

    def test_a_stale_private_proposal_is_rebuilt_from_its_reading_even_after_the_model_is_switched_off(self):
        self.scan_one("Book the movers #private", draft(title="Book the movers", context="#calls", project_key="MOVE"))
        (row,) = self.pending("inbox")
        self.agent.ledger.set_status(row["id"], "stale")
        self.agent.settings = dataclasses.replace(self.agent.settings, private_model_enabled=False)
        calls = len(self.provider.calls)
        rebuilt = self.agent.scan().new_proposals[0]["proposal"]
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual((rebuilt["kind"], rebuilt["private"]), ("next_action", True))

    def test_a_caller_that_says_private_is_believed_even_when_the_text_shows_no_mark(self):
        self.provider.interpretations.append(draft(title="Book the movers", context="#calls"))
        base = {"source": "Book the movers", "reference_date": TODAY.isoformat(), "engine_version": PROPOSAL_VERSION}
        proposal = self.agent._interpret_single(
            self.agent.vault(), "Book the movers", Capture("c1", "Book the movers", 1, False, "x"), base, routing=None,
            reference=TODAY, history=None, previous=None, cached=None, private=True)
        self.assertIs(self.provider.calls[0][1]["private"], True)
        self.assertIs(proposal["private"], True)

    def test_a_private_proposal_offers_no_teacher_feedback(self):
        self.scan_one("Book the movers #private", draft(title="Book the movers", context="#calls"))
        block = self.read("_agent/APPROVAL.md").split("Book the movers #private", 1)[1].split(ITEM_END, 1)[0]
        self.assertNotIn("Teacher feedback", block)

    def test_a_question_from_the_private_model_is_named_as_its_own(self):
        proposal = self.scan_one("Sort out the thing #private", draft(route="needs_clarification", question="Which thing?"))
        self.assertEqual(proposal["kind"], "manual")
        self.assertIn("The private model needs one detail: Which thing?", proposal["reason"])

    def test_a_private_model_that_is_down_is_tried_again_later(self):
        with mock.patch("gtd_agent.ledger._now", return_value="2026-09-28T07:00:00"):
            proposal = self.scan_one("Book the movers #private", down)
        self.assertEqual((proposal["kind"], proposal["private"], proposal["retry"]), ("manual", True, True))
        self.assertIn("The private model request failed", proposal["reason"])
        self.assertIn("It is tried again in a few minutes", proposal["reason"])
        self.assertIn("Edit the Inbox line to try again.", proposal["clarification"]["explanation"])
        self.provider.interpretations.append(draft(title="Book the movers", context="#calls"))
        (again,) = self.agent.scan().new_proposals
        self.assertEqual(again["proposal"]["kind"], "next_action")

    def test_a_model_that_is_switched_off_is_not_asked_again(self):
        with mock.patch("gtd_agent.ledger._now", return_value="2026-09-28T07:00:00"):
            self.scan_one("Book the movers #private", down)
        self.agent.settings = dataclasses.replace(self.agent.settings, private_model_enabled=False)
        self.assertEqual(self.agent.scan().new_proposals, [])

    def test_it_is_not_tried_again_within_ten_minutes(self):
        self.scan_one("Book the movers #private", down)
        self.assertEqual(self.agent.scan().new_proposals, [])
        self.assertEqual(len(self.provider.calls), 1)

    def test_a_stale_private_proposal_is_rebuilt_from_its_saved_reading_by_no_model(self):
        self.scan_one("Asked the landlord about the deposit #private", COMPLETE)
        path = "02_PROJECTS/MOVE/MOVE.md"
        self.write(path, self.read(path).replace("- [ ] Ask the landlord", "- [x] Ask the landlord"))  # you ticked it
        self.tick()
        self.assertIn("Not applied", self.agent.sync_review_requests()[0])
        calls = len(self.provider.calls)
        rebuilt = self.agent.scan().new_proposals[0]["proposal"]
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual((rebuilt["kind"], rebuilt.get("private")), ("manual", True))


class PrivateTrustTests(PrivateModelCase):
    """The private model has no track record: its reading of an item waits for your tick, whatever kinds you trust."""

    def extra_config(self):
        return super().extra_config() + '[trust]\nbookkeeping = ["next_action"]\n'

    def test_its_reading_is_never_applied_on_its_own(self):
        self.scan_one("Book the movers #private", draft(title="Book the movers", context="#calls", project_key="MOVE"))
        self.assertEqual(len(self.pending("inbox")), 1)
        self.assertNotIn("Book the movers", self.read("02_PROJECTS/MOVE/MOVE.md"))

    def test_vault_syntax_still_is(self):
        self.add_inbox("Buy stamps #errands #private")
        self.agent.scan()
        self.assertEqual(self.pending("inbox"), [])
        self.assertIn("Buy stamps #errands #private", self.read("01_GTD/SINGLE_ACTIONS.md"))


class PrivateCaptureRemoteTests(PrivateCaptureTests):
    """With remote inference on, a capture that is not private goes the old way; a private one still never does."""
    remote = True

    def test_a_capture_that_is_not_private_still_learns(self):
        self.agent.ledger.import_learning("old", {"capture": "Book the movers for the move",
                                                  "lesson": "Movers are booked by phone."}, "2026-09-01T00:00:00")
        self.scan_one("Book the movers", draft(title="Book the movers", context="#calls"))
        self.assertEqual([e["lesson"] for e in self.provider.calls[1][1]["examples"]], ["Movers are booked by phone."])

    def test_a_capture_that_is_not_private_never_sees_private_things(self):
        self.scan_one("Ask the landlord about the deposit", draft(title="Ask the landlord", context="#calls"))
        self.assertEqual([name for name, _ in self.provider.calls], ["decide", "interpret"])
        offered = self.provider.calls[1][1]
        self.assertNotIn("private", offered)
        self.assertNotIn("MOVE", [p["key"] for p in offered["projects"]])
        self.assertNotIn("Ask the landlord about the deposit", [i["title"] for i in offered["items"]])

    def test_teacher_feedback_on_a_private_item_is_refused(self):
        self.scan_one("Book the movers #private", draft(title="Book the movers", context="#calls"))
        (row,) = self.pending("inbox")
        with self.assertRaisesRegex(ApprovalError, "Private items take no Teacher feedback"):
            self.agent.submit_teacher_feedback(row["id"], "Make it a project", "")
        self.assertEqual(len(self.provider.calls), 1)

    def test_teacher_feedback_that_links_a_private_project_goes_to_the_private_model_not_the_remote_one(self):
        self.scan_one("Email Sam", draft(title="Email Sam"))
        (row,) = self.pending("inbox")
        calls = len(self.provider.calls)
        self.provider.interpretations.append(draft(title="Email Sam", context="#calls", project_key="MOVE"))
        text = self.read("_agent/APPROVAL.md")
        marker = f"<!-- gtd-agent:proposal id={row['id']} -->"
        start = text.index(marker) + len(marker)
        end = text.index(ITEM_END, start)
        body = with_feedback(text[start:end], "It belongs with [[MOVE]].").replace("- [ ] Teacher feedback",
                                                                                 "- [x] Teacher feedback")
        self.write("_agent/APPROVAL.md", text[:start] + "\n" + body + "\n" + text[end:])
        self.assertIn("revised proposal", self.agent.sync_review_requests()[0])
        later = self.provider.calls[calls:]
        self.assertEqual([name for name, _ in later], ["interpret"])
        self.assertIs(later[0][1]["private"], True)


class PrivateFilingTests(PrivateModelCase):
    """Wherever the private model files an item, it stays private: what is written carries #private."""

    def test_a_next_action_keeps_the_tag(self):
        self.approve("Book the movers #private", draft(title="Book the movers", context="#calls"))
        self.assertRegex(self.read("01_GTD/SINGLE_ACTIONS.md"), r"Book the movers #calls #private ")

    def test_a_waiting_item_keeps_the_tag(self):
        self.approve("Waiting for the landlord to send the deposit back #private",
                     draft(route="waiting_for", person="The landlord", what="Deposit back"))
        self.assertIn("#waiting The landlord: Deposit back #private ", self.read("01_GTD/SINGLE_ACTIONS.md"))

    def test_a_someday_item_keeps_the_tag(self):
        self.approve("Learn pottery one day #private", draft(route="someday_maybe", title="Learn pottery"))
        self.assertIn("\n- Learn pottery #private\n", self.read("01_GTD/SOMEDAY_MAYBE.md"))

    def test_a_someday_item_with_a_review_date_keeps_the_tag_after_it(self):
        self.approve("Learn pottery in a year #private", draft(route="someday_maybe", title="Learn pottery",
                                                              start_evidence="in a year"))
        self.assertIn("\n- Learn pottery · review 2027-09-28 #private\n", self.read("01_GTD/SOMEDAY_MAYBE.md"))

    def test_a_log_line_keeps_the_tag(self):
        self.approve("The lab spending is over budget #private",
                     draft(route="project_note", project_key="DEMO_PROJECT", title="Lab spending is over budget"))
        self.assertIn("\n- 2026-09-28 Lab spending is over budget #private\n",
                      self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"))

    def test_a_new_project_is_private(self):
        self.approve("Plan the move to the new flat, Measure the rooms first #private",
                     draft(route="new_project", name_excerpt="Plan the move", title="Move to the new flat",
                           first_step="Measure the rooms", context="#anywhere"))
        note = self.read("02_PROJECTS/PLAN_THE_MOVE/PLAN_THE_MOVE.md")
        self.assertIn("\nprivate: true\n", note)
        self.assertNotIn("private: false", note)

    def test_an_item_private_only_by_its_link_is_tagged_too(self):
        self.approve("Think about the lease [[MOVE]]",
                     draft(title="Reread the lease", context="#anywhere", project_key="DEMO_PROJECT"))
        self.assertRegex(self.read("02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"), r"Reread the lease #anywhere #private ")

    def test_a_someday_item_written_in_vault_syntax_keeps_the_tag_too(self):
        self.add_inbox("Learn pottery #someday #private")
        self.agent.scan()
        self.tick()
        self.agent.sync_review_requests()
        self.assertIn("\n- Learn pottery #private\n", self.read("01_GTD/SOMEDAY_MAYBE.md"))


class CommandCase(VaultCase):
    """doctor and probe-private against a stand-in for the private model."""
    config = ""   # more keys for [private_model]

    def setUp(self) -> None:
        self.model = LocalModel()
        self.addCleanup(self.model.stop)
        env = mock.patch.dict(os.environ, {"GTD_SECRET_LOCAL_MODEL": "local-key"})
        env.start()
        self.addCleanup(env.stop)
        super().setUp()

    def extra_config(self):
        return "[private_model]\n" + on(url=f'"{self.model.url}"') + self.config

    def command(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--config", str(self.settings.config_path), *args])
        return code, out.getvalue(), err.getvalue()


class DoctorTests(CommandCase):
    def test_a_reachable_private_model_is_named_by_its_model_and_address(self):
        _, out, err = self.command("doctor")
        self.assertEqual(err, "")  # nothing went wrong while checking
        self.assertIn(f"OK: Private model reachable · qwen3:8b at 127.0.0.1:{self.model.server.server_port}", out)
        self.assertEqual(self.model.requests, [])  # the check only connects: it sends nothing
        self.assertNotIn("Key '", out)  # no key is named, so none is asked after

    def test_one_that_is_down_says_so_and_what_happens_meanwhile(self):
        port = self.model.server.server_port
        self.model.stop()
        code, out, _ = self.command("doctor")
        self.assertIn(f"CHECK: Private model reachable · 127.0.0.1:{port}: ConnectionRefusedError", out)
        self.assertIn("private items wait for you", out)
        self.assertEqual(code, 1)


class DoctorDefaultHttpPortTests(CommandCase):
    url = "http://127.0.0.1/v1"
    port = 80

    def extra_config(self):
        return "[private_model]\n" + on(url=f'"{self.url}"')

    def test_a_url_without_a_port_is_named_at_the_default_one(self):
        with mock.patch("gtd_agent.cli.knock"):  # nothing listens there; the name is what is checked
            _, out, _ = self.command("doctor")
        self.assertIn(f"OK: Private model reachable · qwen3:8b at 127.0.0.1:{self.port}", out)


class DoctorDefaultHttpsPortTests(DoctorDefaultHttpPortTests):
    url = "https://127.0.0.1/v1"
    port = 443


class DoctorKeyTests(CommandCase):
    config = 'key = "local-model"\n'

    def test_a_saved_key_is_acknowledged_without_being_shown(self):
        _, out, _ = self.command("doctor")
        self.assertIn("OK: Key 'local-model' for the private model", out)
        self.assertNotIn("local-key", out)

    def test_a_key_that_is_not_saved_is_named(self):
        with mock.patch.dict(os.environ, {"GTD_SECRET_LOCAL_MODEL": ""}):
            _, out, _ = self.command("doctor")
        self.assertIn("CHECK: Key 'local-model' for the private model · missing: run set-secret local-model", out)


class DoctorOffTests(VaultCase):
    def test_nothing_is_said_while_it_is_off(self):
        out = io.StringIO()
        with redirect_stdout(out):
            main(["--config", str(self.settings.config_path), "doctor"])
        self.assertNotIn("Private model", out.getvalue())


class ProbeTests(CommandCase):
    def test_it_sends_one_made_up_request_and_prints_the_answer(self):
        self.model.replies.append((200, completion({"route": "next_action", "title": "Call the dentist"}), {}))
        code, out, err = self.command("probe-private")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Private model: {'route': 'next_action', 'title': 'Call the dentist'}", out)
        (_, _, body), = self.model.requests
        sent = body["messages"][1]["content"]
        self.assertIn("Call the dentist to schedule a cleaning", sent)
        self.assertNotIn("DEMO_PROJECT", sent)  # nothing from the vault
        self.assertNotIn("AREA_ADMIN", sent)

    def test_a_model_that_is_down_is_a_check_not_a_traceback(self):
        self.model.stop()
        code, out, err = self.command("probe-private")
        self.assertEqual(code, 1)
        self.assertIn("CHECK: The private model request failed", err)


class ProbeOffTests(VaultCase):
    def test_it_says_how_to_turn_it_on(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--config", str(self.settings.config_path), "probe-private"])
        self.assertEqual(code, 1)
        self.assertIn("CHECK: The private model is off. Set enabled = true under [private_model]", err.getvalue())


class PrivateModelOffTests(VaultCase):
    """With the private model off, or not listed for this job, a private capture goes to no model at all."""
    remote = True
    config = ""

    def extra_config(self):
        return self.config

    def test_a_private_capture_stays_manual_and_goes_to_no_model(self):
        self.add_inbox("Book the movers #private")
        (item,) = self.agent.scan().new_proposals
        self.assertEqual(item["proposal"]["kind"], "manual")
        self.assertIn("Private capture. Write it in vault syntax", item["proposal"]["reason"])
        self.assertEqual((item["proposal"]["private"], item["proposal"]["no_model"]), (True, True))
        self.assertEqual(self.provider.calls, [])


class PrivateModelSwitchedOffTests(PrivateModelOffTests):
    config = "[private_model]\n" + on(enabled="false")


class PrivateModelUnlistedTests(PrivateModelOffTests):
    config = "[private_model]\n" + on(jobs="[]")


if __name__ == "__main__":
    unittest.main()
