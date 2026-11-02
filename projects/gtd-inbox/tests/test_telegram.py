"""Telegram: capture from your own chat, and what the agent sends back (phase 3 item 5).

Nothing here reaches Telegram. `FakeBot` stands in for the Bot API: updates go in, every call is kept.
"""
import io
import os
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date
from unittest.mock import patch

from helpers import VaultCase, make_settings
from gtd_agent.cli import main
from gtd_agent.telegram import TelegramCapture, TelegramError


class FakeBot:
    """The Telegram Bot API as far as the agent uses it: queued updates in, every call kept."""

    def __init__(self, *updates: dict):
        self.updates = list(updates)
        self.calls: list[tuple[str, dict]] = []
        self.broken: Exception | None = None  # raised by sendMessage: the message is not delivered
        self.refused: list[dict] = []         # what sendMessage was asked to send while it was broken

    def __call__(self, token, method, params=None, timeout=15):
        if method == "sendMessage" and self.broken is not None:
            self.refused.append(dict(params or {}))
            raise self.broken
        self.calls.append((method, dict(params or {})))
        if method == "getUpdates":
            return self.updates
        return {}

    def sent(self) -> list[dict]:
        return [params for method, params in self.calls if method == "sendMessage"]

    def texts(self) -> list[str]:
        return [params["text"] for params in self.sent()]


def update(number: int, text: str | None = None, chat: int = 42, **fields) -> dict:
    message = {"message_id": number, "chat": {"id": chat}, **fields}
    if text is not None:
        message["text"] = text
    return {"update_id": number, "message": message}


def edit(number: int, text: str, chat: int = 42) -> dict:
    return {"update_id": number, "edited_message": {"message_id": number - 1, "chat": {"id": chat}, "text": text}}


class TelegramCase(VaultCase):
    telegram_config = ""

    def extra_config(self) -> str:
        return "[telegram]\nenabled = true\nchat_id = 42\n" + self.telegram_config

    def poll(self, *updates: dict) -> FakeBot:
        """One poll of the fake bot. `self.messages` is what the agent reports."""
        bot = FakeBot(*updates)
        with patch("gtd_agent.telegram.get_secret", return_value="token"), patch("gtd_agent.telegram.call", bot):
            self.messages = TelegramCapture(self.agent).poll()
        return bot

    def notices(self, chat: int) -> list[str]:
        return [m for m in self.messages if f"from chat {chat}" in m]


class TelegramTests(VaultCase):
    def setUp(self):
        super().setUp()
        config = self.settings.config_path
        config.write_text(config.read_text() + "[telegram]\nenabled = true\nchat_id = 42\n", encoding="utf-8")
        from gtd_agent.core import Settings
        self.agent.settings = Settings.load(config)
        self.capture = TelegramCapture(self.agent)

    def test_only_your_chat_is_captured(self):
        sent = []
        updates = [{"update_id": 7, "message": {"message_id": 1, "chat": {"id": 42}, "text": "Buy stamps #errands\nCall Sam"}},
                   {"update_id": 8, "message": {"message_id": 2, "chat": {"id": 99}, "text": "spam"}}]

        def fake_call(token, method, params=None, timeout=15):
            if method == "getUpdates":
                self.assertEqual(params["offset"], 0)
                return updates
            sent.append((method, params))
            return {}

        with patch("gtd_agent.telegram.get_secret", return_value="token"), \
                patch("gtd_agent.telegram.call", side_effect=fake_call):
            messages = self.capture.poll()
        inbox = self.read("00_INBOX/INBOX.md")
        self.assertIn("- Buy stamps #errands\n- Call Sam\n", inbox)
        self.assertNotIn("spam", inbox)
        self.assertIn("Telegram: captured 2 Inbox lines", messages)
        self.assertTrue(any("ignored a Telegram message from chat 99" in m for m in messages))
        self.assertEqual(self.agent.ledger.kv_get("telegram_offset"), "9")
        self.assertEqual(sent[0][1]["text"], "Captured 2 lines to your Inbox.")

    def test_missing_token_is_reported(self):
        with patch("gtd_agent.telegram.get_secret", return_value=None):
            self.assertIn("no bot token", self.capture.poll()[0])


class TelegramQuietInboxTests(VaultCase):
    inbox_quiet = 30

    def extra_config(self) -> str:
        return "[telegram]\nenabled = true\nchat_id = 42\n"

    def test_a_telegram_capture_is_read_at_once(self):
        self.inbox_untouched(600)
        updates = [{"update_id": 7, "message": {"message_id": 1, "chat": {"id": 42}, "text": "Buy stamps #errands"}}]
        with patch("gtd_agent.telegram.get_secret", return_value="token"), \
                patch("gtd_agent.telegram.call", side_effect=lambda token, method, params=None, timeout=15:
                      updates if method == "getUpdates" else {}):
            TelegramCapture(self.agent).poll()
        self.assertEqual(len(self.agent.scan().new_proposals), 1)  # the agent's own write is not your typing


class CaptionTests(TelegramCase):
    def test_a_photo_with_a_caption_is_captured_as_its_caption(self):
        bot = self.poll(update(1, photo=[{"file_id": "a"}], caption="Ask Sam about the receipt #errands"))
        self.assertIn("- Ask Sam about the receipt #errands\n", self.read("00_INBOX/INBOX.md"))
        self.assertEqual(bot.texts(), ["Captured 1 line to your Inbox. The photo or file is not saved, only its caption."])
        self.assertIn("Telegram: captured 1 Inbox line", self.messages)

    def test_a_file_with_a_two_line_caption_is_two_lines(self):
        bot = self.poll(update(1, document={"file_id": "a"}, caption="Sign the form\nMail the form #errands"))
        self.assertIn("- Sign the form\n- Mail the form #errands\n", self.read("00_INBOX/INBOX.md"))
        self.assertTrue(bot.texts()[0].startswith("Captured 2 lines to your Inbox."))

    def test_a_photo_with_no_caption_says_what_is_captured(self):
        before = self.read("00_INBOX/INBOX.md")
        bot = self.poll(update(1, photo=[{"file_id": "a"}]))
        self.assertEqual(self.read("00_INBOX/INBOX.md"), before)
        self.assertEqual(bot.texts(), ["Only text is captured. A caption on a photo or file is captured too."])
        self.assertFalse(any(m.startswith("Telegram: captured") for m in self.messages))

    def test_a_caption_that_looks_like_a_command_is_still_a_capture(self):
        bot = self.poll(update(1, photo=[{"file_id": "a"}], caption="/help me find a dentist"))
        self.assertIn("- /help me find a dentist\n", self.read("00_INBOX/INBOX.md"))
        self.assertTrue(bot.texts()[0].startswith("Captured 1 line"))

    def test_help_tells_you_about_captions(self):
        bot = self.poll(update(1, "/help"))
        self.assertIn("caption", bot.texts()[0])


class EditTests(TelegramCase):
    def test_the_agent_asks_for_edits_so_it_can_explain_them(self):
        bot = self.poll()
        method, params = bot.calls[0]
        self.assertEqual(method, "getUpdates")
        self.assertEqual(sorted(params["allowed_updates"]), ["edited_message", "message"])

    def test_an_edit_does_not_change_the_inbox_and_says_so(self):
        before = self.read("00_INBOX/INBOX.md")
        bot = self.poll(edit(2, "Buy stamps today"))
        self.assertEqual(self.read("00_INBOX/INBOX.md"), before)
        self.assertEqual(bot.texts(), ["An edit does not change the Inbox. Fix the line there, or send it again."])
        self.assertEqual(self.agent.ledger.kv_get("telegram_offset"), "3")

    def test_an_edit_from_someone_else_is_a_stranger_and_gets_no_answer(self):
        bot = self.poll(edit(2, "Buy stamps today", chat=99))
        self.assertEqual(bot.sent(), [])
        self.assertEqual(len(self.notices(99)), 1)


class StrangerTests(TelegramCase):
    def test_a_stranger_gets_no_answer_and_one_notice_a_day(self):
        first = self.poll(update(1, "hi", chat=99))
        self.assertEqual(first.sent(), [])
        self.assertEqual(len(self.notices(99)), 1)
        self.poll(update(2, "hello again", chat=99), update(3, "and again", chat=99))
        self.assertEqual(self.notices(99), [])
        self.agent._today = date(2026, 9, 29)
        self.poll(update(4, "the next day", chat=99))
        self.assertEqual(len(self.notices(99)), 1)

    def test_each_stranger_is_noticed_once(self):
        self.poll(update(1, "a", chat=98), update(2, "b", chat=99), update(3, "c", chat=98))
        self.assertEqual((len(self.notices(98)), len(self.notices(99))), (1, 1))

    def test_notices_of_old_days_are_forgotten(self):
        self.poll(update(1, "hi", chat=99))
        self.agent._today = date(2026, 10, 20)
        self.poll(update(2, "hi", chat=98))
        self.assertEqual(list(self.agent.ledger.kv_items("telegram_stranger|")), ["telegram_stranger|98"])

    def test_a_stranger_is_never_captured(self):
        before = self.read("00_INBOX/INBOX.md")
        self.poll(update(1, "Buy stamps", chat=99))
        self.assertEqual(self.read("00_INBOX/INBOX.md"), before)


class LongLineTests(TelegramCase):
    """The Inbox skips a line over max_capture_chars without a word, so a line that long is reported back instead."""

    def setUp(self):
        super().setUp()
        self.agent.settings = replace(self.settings, max_capture_chars=30)

    def test_a_line_the_inbox_would_skip_is_reported_not_lost_in_silence(self):
        bot = self.poll(update(1, "Short one\n" + "x" * 31 + "\nAnother one"))
        inbox = self.read("00_INBOX/INBOX.md")
        self.assertIn("- Short one\n- Another one\n", inbox)
        self.assertNotIn("xxx", inbox)
        self.assertEqual(bot.texts(), ["Captured 2 lines to your Inbox. 1 line is over 30 characters and was not "
                                       "captured: send it in shorter pieces."])

    def test_several_lines_over_the_limit_are_counted(self):
        bot = self.poll(update(1, "Short one\n" + "x" * 31 + "\n" + "z" * 40))
        self.assertEqual(bot.texts(), ["Captured 1 line to your Inbox. 2 lines are over 30 characters and were not "
                                       "captured: send them in shorter pieces."])

    def test_a_line_at_the_limit_is_captured(self):
        bot = self.poll(update(1, "y" * 30))
        self.assertIn("- " + "y" * 30 + "\n", self.read("00_INBOX/INBOX.md"))
        self.assertEqual(bot.texts(), ["Captured 1 line to your Inbox."])

    def test_one_line_that_is_too_long_is_not_captured_and_the_reply_says_so(self):
        bot = self.poll(update(1, "x" * 31))
        self.assertEqual(bot.texts(), ["Nothing captured. 1 line is over 30 characters: send it in shorter pieces."])

    def test_when_every_line_is_too_long_nothing_is_captured_and_the_reply_says_so(self):
        before = self.read("00_INBOX/INBOX.md")
        bot = self.poll(update(1, "x" * 31 + "\n" + "z" * 40))
        self.assertEqual(self.read("00_INBOX/INBOX.md"), before)
        self.assertEqual(bot.texts(), ["Nothing captured. 2 lines are over 30 characters: send them in shorter pieces."])
        self.assertFalse(any(m.startswith("Telegram: captured") for m in self.messages))


class SendTests(TelegramCase):
    """What the agent says on its own (pings), as opposed to its answers to your messages."""

    def send(self, text: str, bot: FakeBot | None = None) -> FakeBot:
        bot = bot or FakeBot()
        with patch("gtd_agent.telegram.get_secret", return_value="token"), patch("gtd_agent.telegram.call", bot):
            TelegramCapture(self.agent).send(text)
        return bot

    def test_a_message_goes_to_your_chat_without_a_link_preview(self):
        bot = self.send("Hello")
        self.assertEqual(bot.sent(), [{"chat_id": 42, "text": "Hello", "disable_web_page_preview": True}])

    def test_a_message_longer_than_telegram_allows_is_cut_with_an_ellipsis(self):
        text = self.send("x" * 5000).texts()[0]
        self.assertEqual((len(text), text[-1]), (4096, "…"))

    def test_a_message_of_the_longest_size_is_sent_whole(self):
        self.assertEqual(self.send("y" * 4096).texts()[0], "y" * 4096)

    def test_a_refusal_reaches_the_caller(self):
        bot = FakeBot()
        bot.broken = TelegramError("Telegram refused sendMessage: Forbidden")
        with self.assertRaises(TelegramError):
            self.send("Hello", bot)

    def test_without_a_token_nothing_is_sent_and_it_says_why(self):
        bot = FakeBot()
        with patch("gtd_agent.telegram.get_secret", return_value=None), patch("gtd_agent.telegram.call", bot):
            with self.assertRaisesRegex(TelegramError, "no Telegram bot token"):
                TelegramCapture(self.agent).send("Hello")
        self.assertEqual(bot.calls, [])

    def test_ready_needs_the_switch_the_chat_and_the_token(self):
        def ready(token="token") -> bool:
            with patch("gtd_agent.telegram.get_secret", return_value=token):
                return TelegramCapture(self.agent).ready()
        self.assertTrue(ready())
        self.assertFalse(ready(None))
        self.agent.settings = replace(self.settings, telegram_chat_id=0)
        self.assertFalse(ready())
        self.agent.settings = replace(self.settings, telegram_enabled=False)
        self.assertFalse(ready())


class HelloTests(TelegramCase):
    """run.ps1 telegram test says one thing to your chat, so you can see that pings will arrive."""

    def hello(self, token: str | None = "token", bot: FakeBot | None = None) -> tuple[list[str], FakeBot]:
        bot = bot or FakeBot()
        with patch("gtd_agent.telegram.get_secret", return_value=token), patch("gtd_agent.telegram.call", bot):
            return TelegramCapture(self.agent).hello(), bot

    def test_it_sends_one_message_to_your_chat_and_says_so(self):
        lines, bot = self.hello()
        self.assertEqual(lines, ["Sent. Look for it on your phone."])
        self.assertEqual(bot.texts(), ["GTD agent: test message. Pings will reach this chat."])

    def test_a_message_that_could_not_be_sent_is_reported_without_a_traceback(self):
        bot = FakeBot()
        bot.broken = TelegramError("Telegram refused sendMessage: Forbidden")
        lines, _ = self.hello(bot=bot)
        self.assertEqual(lines, ["CHECK: Telegram refused sendMessage: Forbidden"])

    def test_without_a_token_it_says_how_to_save_one(self):
        lines, bot = self.hello(token=None)
        self.assertEqual(lines, ["No bot token saved. Run: run.ps1 set-secret telegram"])
        self.assertEqual(bot.calls, [])

    def test_without_a_chat_it_says_how_to_find_it(self):
        self.agent.settings = replace(self.settings, telegram_chat_id=0)
        lines, bot = self.hello()
        self.assertEqual(lines, ["No chat id yet. Run: run.ps1 telegram setup, then put the number under [telegram] chat_id."])
        self.assertEqual(bot.calls, [])

    def test_with_telegram_off_it_says_how_to_turn_it_on(self):
        self.agent.settings = replace(self.settings, telegram_enabled=False)
        lines, bot = self.hello()
        self.assertEqual(lines, ["Telegram is off. Set enabled = true under [telegram] in config.toml."])
        self.assertEqual(bot.calls, [])


class DoctorCase(TelegramCase):
    """run.ps1 doctor says which pings are on, so a mistake in the [telegram] table shows before a ping is missed."""

    def lines(self) -> list[str]:
        out = io.StringIO()
        with redirect_stdout(out), patch.dict(os.environ, {"GTD_TELEGRAM_TOKEN": "token"}):
            main(["--config", str(self.settings.config_path), "doctor"])
        return [line for line in out.getvalue().splitlines() if "Telegram" in line]


class DoctorTests(DoctorCase):
    def test_the_default_pings_and_the_quiet_hours_are_listed(self):
        self.assertEqual(self.lines(), ["OK: Telegram bot token saved", "OK: Telegram chat_id set",
                                        "OK: Telegram pings: brief (07:30), approvals, bridge · quiet 22:00–07:00"])


class DoctorSettingsTests(DoctorCase):
    telegram_config = ('pings = ["bridge", "brief"]\nbrief_time = "08:05"\nquiet_start = "23:30"\nquiet_end = "06:45"\n')

    def test_a_changed_list_brief_time_and_quiet_hours_are_listed(self):
        self.assertEqual(self.lines()[2], "OK: Telegram pings: bridge, brief (08:05) · quiet 23:30–06:45")


class DoctorNoPingsTests(DoctorCase):
    telegram_config = "pings = []\n"

    def test_no_pings_is_said_plainly(self):
        self.assertEqual(self.lines()[2], "OK: Telegram pings: none, capture only")


class HelloCommandTests(TelegramCase):
    def test_the_command_prints_what_it_found(self):
        bot = FakeBot()
        out = io.StringIO()
        with redirect_stdout(out), patch("gtd_agent.telegram.get_secret", return_value="token"), \
                patch("gtd_agent.telegram.call", bot):
            code = main(["--config", str(self.settings.config_path), "telegram", "test"])
        self.assertEqual((code, out.getvalue().splitlines()), (0, ["Sent. Look for it on your phone."]))
        self.assertEqual(len(bot.texts()), 1)


if __name__ == "__main__":
    unittest.main()
