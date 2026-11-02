"""Offline checks for relay host's Telegram spool and vault computer's Syncthing reader."""
import json
import io
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from helpers import VaultCase, make_settings
from gtd_agent.cli import main
from gtd_agent.runner import loop_operations
from gtd_agent.telegram import RelayCapture
from tools.telegram_relay import Relay, load_env


def update(number, text=None, chat=42, **fields):
    message = {"message_id": number, "chat": {"id": chat}, **fields}
    if text is not None:
        message["text"] = text
    return {"update_id": number, "message": message}


class RelayWriterTests(VaultCase):
    def setUp(self):
        super().setUp()
        self.spool = self.base / "syncthing" / "telegram"
        self.state = self.base / "relay-state"
        self.calls = []
        self.updates = []

        def api(token, method, params=None, timeout=15):
            self.calls.append((method, params))
            if method == "getUpdates":
                return [row for row in self.updates if row["update_id"] >= params["offset"]]
            return {}

        self.relay = Relay("fake-token", 42, self.spool, self.state, api=api)

    def test_only_own_text_and_caption_are_spooled_before_acknowledgment(self):
        self.updates = [update(7, "Buy stamps", chat=99), update(8, "Call Sam"),
                        update(9, caption="Book room"),
                        {"update_id": 10, "edited_message": {"message_id": 9, "chat": {"id": 42},
                                                                "text": "changed"}}]
        self.assertEqual(self.relay.poll_once(), 4)
        self.assertEqual(sorted(p.name for p in self.spool.glob("*.json")), ["8.json", "9.json"])
        self.assertEqual(json.loads((self.spool / "8.json").read_text())["text"], "Call Sam")
        self.assertEqual(json.loads((self.spool / "9.json").read_text())["text"], "Book room")
        self.assertEqual((self.state / "offset.txt").read_text(), "11\n")
        self.assertEqual([name for name, _ in self.calls].count("sendMessage"), 3)

    def test_replayed_update_keeps_one_file(self):
        self.updates = [update(12, "Call Sam")]
        self.relay.poll_once()
        (self.state / "offset.txt").write_text("0\n")  # crash before offset was durable
        self.relay.poll_once()
        self.assertEqual(len(list(self.spool.glob("*.json"))), 1)
        self.assertEqual((self.state / "offset.txt").read_text(), "13\n")

    def test_failed_spool_write_does_not_acknowledge_update(self):
        self.updates = [update(3, "Call Sam")]
        self.spool.mkdir(parents=True)
        (self.spool / "3.json").mkdir()  # cannot atomically replace directory
        with self.assertRaises(OSError):
            self.relay.poll_once()
        self.assertFalse((self.state / "offset.txt").exists())

    def test_corrupt_existing_capture_is_never_acknowledged(self):
        self.updates = [update(4, "Call Sam")]
        self.spool.mkdir(parents=True)
        (self.spool / "4.json").write_text("corrupt", encoding="utf-8")
        with self.assertRaises(OSError):
            self.relay.poll_once()
        self.assertFalse((self.state / "offset.txt").exists())


class RelayReaderTests(VaultCase):
    def setUp(self):
        super().setUp()
        self.spool = self.base / "syncthing" / "telegram"
        self.spool.mkdir(parents=True)
        self.agent.settings = replace(self.settings, telegram_enabled=True, telegram_chat_id=42,
                                      telegram_capture="relay", telegram_relay_folder=self.spool)

    def put(self, number, text, chat=42):
        (self.spool / f"{number}.json").write_text(json.dumps({"version": 1, "update_id": number,
                                                                  "chat_id": chat, "text": text}), encoding="utf-8")

    def test_synced_capture_is_appended_once_and_wakes_inbox_scan(self):
        self.put(8, "Buy stamps #errands\nCall Sam")
        operations = loop_operations(self.agent)
        self.assertIn("Telegram relay", [op.label for op in operations])
        self.assertNotIn("Telegram", [op.label for op in operations])
        messages = RelayCapture(self.agent).poll()
        self.assertEqual(messages, ["Telegram: captured 2 Inbox lines"])
        self.assertEqual(RelayCapture(self.agent).poll(), [])
        self.assertIn("- Buy stamps #errands\n- Call Sam\n", self.read("00_INBOX/INBOX.md"))
        self.assertEqual(len(self.agent.scan().new_proposals), 2)

    def test_untrusted_files_and_wrong_chat_never_enter_inbox(self):
        self.put(9, "Stranger", chat=99)
        (self.spool / "10.json").write_text('{"version":1,"update_id":11,"chat_id":42,"text":"bad"}')
        (self.spool / "11.json").write_text("not json")
        (self.spool / "12.json").write_bytes(b"X" * 50000)
        self.assertTrue(any("CHECK" in line for line in RelayCapture(self.agent).poll()))
        self.assertNotIn("Stranger", self.read("00_INBOX/INBOX.md"))
        self.assertNotIn("bad", self.read("00_INBOX/INBOX.md"))

    def test_files_enter_in_update_order(self):
        self.put(10, "Second")
        self.put(9, "First")
        RelayCapture(self.agent).poll()
        inbox = self.read("00_INBOX/INBOX.md")
        self.assertLess(inbox.index("First"), inbox.index("Second"))

    def test_missing_syncthing_folder_reports_check(self):
        self.agent.settings = replace(self.agent.settings, telegram_relay_folder=self.base / "missing")
        self.assertIn("CHECK", RelayCapture(self.agent).poll()[0])

    def test_manual_poll_reads_files_without_polling_bot(self):
        self.put(13, "Call Sam")
        config = self.settings.config_path
        with config.open("a", encoding="utf-8") as stream:
            stream.write(f'\n[telegram]\nenabled = true\nchat_id = 42\ncapture = "relay"\n'
                         f'relay_folder = "{self.spool.as_posix()}"\n')
        with patch("gtd_agent.telegram.call", side_effect=AssertionError("bot polled")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--config", str(config), "telegram", "poll"]), 0)
        self.assertIn("- Call Sam", self.read("00_INBOX/INBOX.md"))

    def test_crash_after_inbox_write_recovers_without_duplicate(self):
        self.put(15, "Buy stamps")
        ledger = self.agent.ledger
        real_set = ledger.kv_set

        def fail_receipt(key, value):
            if key == "telegram_relay|15":
                raise OSError("simulated crash")
            return real_set(key, value)

        with patch.object(ledger, "kv_set", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                RelayCapture(self.agent).poll()
        RelayCapture(self.agent).poll()
        self.assertEqual(self.read("00_INBOX/INBOX.md").count("- Buy stamps\n"), 1)
        self.assertNotIn("gtd-agent:telegram-relay", self.read("00_INBOX/INBOX.md"))


class RelayConfigTests(VaultCase):
    def test_relay_mode_needs_folder(self):
        with self.assertRaisesRegex(ValueError, "relay_folder"):
            make_settings(self.base, extra='[telegram]\ncapture = "relay"\n')

    def test_relay_folder_cannot_sync_agent_state(self):
        with self.assertRaisesRegex(ValueError, "state"):
            make_settings(self.base, extra=f'[telegram]\ncapture = "relay"\n'
                                         f'relay_folder = "{self.base.as_posix()}"\n')

    def test_secret_file_cannot_be_inside_syncthing_folder(self):
        spool = self.base / "spool"
        spool.mkdir()
        env = spool / "telegram.env"
        env.write_text(f"GTD_TELEGRAM_TOKEN=fake\nGTD_TELEGRAM_CHAT_ID=42\n"
                       f"GTD_TELEGRAM_SPOOL={spool.as_posix()}\n"
                       f"GTD_TELEGRAM_STATE={(self.base / 'state2').as_posix()}\n")
        with self.assertRaisesRegex(ValueError, "outside Syncthing"):
            load_env(env)


if __name__ == "__main__":
    unittest.main()
