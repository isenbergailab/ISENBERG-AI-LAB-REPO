import unittest
from unittest.mock import patch

from helpers import VaultCase, make_settings
from gtd_agent.telegram import TelegramCapture


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


if __name__ == "__main__":
    unittest.main()
