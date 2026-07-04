"""Unit tests for Discord integration — AIResponseHandler reply_cb and prompt_override."""
import json
import queue
import threading
import unittest
from unittest.mock import MagicMock, patch


class FakeTTS:
    def speak(self, text): pass


class TestAIHandlerReplyCallback(unittest.TestCase):
    def _make_handler(self, fake_reply="Hello from AI"):
        """Build an AIResponseHandler with requests.post mocked to return fake_reply."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

        import twitch_bot

        cfg = {
            "provider": "Ollama",
            "endpoint": "http://localhost:11434/v1/chat/completions",
            "model": "llama3",
            "api_key": "",
            "system_prompt": "You are a test bot.",
            "tts_ai": False,
        }

        handler = twitch_bot.AIResponseHandler(
            get_config=lambda: cfg,
            log=lambda msg: None,
            tts=FakeTTS(),
        )

        # Patch out the HTTP call with a streaming mock
        mock_resp = MagicMock()
        chunk = json.dumps({"choices": [{"delta": {"content": fake_reply}, "finish_reason": None}]})
        mock_resp.iter_lines.return_value = iter([
            b"data: " + chunk.encode(),
            b"data: [DONE]",
        ])
        mock_resp.raise_for_status = MagicMock()
        handler._mock_patch = patch("requests.post", return_value=mock_resp)
        handler._mock_patch.start()
        self.addCleanup(handler._mock_patch.stop)
        return handler

    def tearDown(self):
        pass  # patches are stopped per-handler

    def test_reply_cb_called_with_ai_response(self):
        """reply_cb receives the AI reply text."""
        handler = self._make_handler("Hello from AI")
        received = []
        done = threading.Event()

        def cb(text):
            received.append(text)
            done.set()

        handler.handle("testuser", "hi there", reply_cb=cb)
        done.wait(timeout=5)

        self.assertEqual(received, ["Hello from AI"])

    def test_reply_cb_none_does_not_crash(self):
        """Passing no reply_cb (Twitch path) still works."""
        handler = self._make_handler("AI says hi")
        done = threading.Event()

        original_query = handler._query

        def wrapped(*args, **kwargs):
            try:
                original_query(*args, **kwargs)
            finally:
                done.set()

        handler._query = wrapped
        handler.handle("twitchuser", "hello")
        self.assertTrue(done.wait(timeout=5), "AI worker did not complete within 5 s")

    def test_prompt_override_used_when_provided(self):
        """prompt_override replaces the system_prompt from get_config."""
        import requests as req

        handler = self._make_handler("OK")
        captured_payload = []

        def fake_post(url, headers=None, json=None, timeout=None, stream=None):
            captured_payload.append(json)
            mock_resp = MagicMock()
            chunk = __import__("json").dumps({"choices": [{"delta": {"content": "OK"}, "finish_reason": None}]})
            mock_resp.iter_lines.return_value = iter([
                b"data: " + chunk.encode(),
                b"data: [DONE]",
            ])
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        handler._mock_patch.stop()
        done = threading.Event()

        with patch("requests.post", side_effect=fake_post):
            original_query = handler._query

            def wrapped(*args, **kwargs):
                try:
                    original_query(*args, **kwargs)
                finally:
                    done.set()

            handler._query = wrapped
            handler.handle("user", "msg", prompt_override="Discord-specific prompt")
            done.wait(timeout=5)

        self.assertTrue(len(captured_payload) > 0)
        messages = captured_payload[0]["messages"]
        system_msg = next((m for m in messages if m["role"] == "system"), None)
        self.assertIsNotNone(system_msg)
        self.assertEqual(system_msg["content"], "Discord-specific prompt")


class TestDiscordClientTriggerMode(unittest.TestCase):
    def _make_message(self, bot_user, mentions=None, is_reply_to_bot=False):
        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.name = "testuser"
        msg.content = "hello"
        msg.mentions = mentions or []

        if is_reply_to_bot:
            msg.reference = MagicMock()
            msg.reference.resolved = MagicMock()
            msg.reference.resolved.author = bot_user
        else:
            msg.reference = None

        return msg

    def _is_triggered(self, trigger, bot_user, message):
        """Mirror of DiscordClient._is_triggered static method."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot
        return twitch_bot.DiscordClient._is_triggered(trigger, bot_user, message)

    def test_all_messages_always_triggers(self):
        bot = MagicMock()
        msg = self._make_message(bot)
        self.assertTrue(self._is_triggered("All messages", bot, msg))

    def test_all_plus_mentions_always_triggers(self):
        bot = MagicMock()
        msg = self._make_message(bot)
        self.assertTrue(self._is_triggered("All messages + mentions + replies", bot, msg))

    def test_mention_only_triggers_on_mention(self):
        bot = MagicMock()
        msg = self._make_message(bot, mentions=[bot])
        self.assertTrue(self._is_triggered("@mention only", bot, msg))

    def test_mention_only_no_trigger_without_mention(self):
        bot = MagicMock()
        msg = self._make_message(bot, mentions=[])
        self.assertFalse(self._is_triggered("@mention only", bot, msg))

    def test_mention_plus_replies_triggers_on_mention(self):
        bot = MagicMock()
        msg = self._make_message(bot, mentions=[bot])
        self.assertTrue(self._is_triggered("@mention + replies", bot, msg))

    def test_mention_plus_replies_triggers_on_reply(self):
        bot = MagicMock()
        msg = self._make_message(bot, mentions=[], is_reply_to_bot=True)
        self.assertTrue(self._is_triggered("@mention + replies", bot, msg))

    def test_mention_plus_replies_no_trigger_for_plain_message(self):
        bot = MagicMock()
        msg = self._make_message(bot, mentions=[], is_reply_to_bot=False)
        self.assertFalse(self._is_triggered("@mention + replies", bot, msg))

    def test_mention_plus_replies_no_trigger_for_reply_to_other_user(self):
        bot = MagicMock()
        other_user = MagicMock()
        msg = self._make_message(bot, mentions=[])
        msg.reference = MagicMock()
        msg.reference.resolved = MagicMock()
        msg.reference.resolved.author = other_user
        self.assertFalse(self._is_triggered("@mention + replies", bot, msg))


if __name__ == "__main__":
    unittest.main()
