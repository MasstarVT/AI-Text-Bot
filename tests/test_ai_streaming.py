"""Unit tests for AIResponseHandler streaming sentence dispatch."""
import io
import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch


def make_openai_stream(*tokens):
    """Build a fake OpenAI SSE stream from a list of token strings."""
    lines = []
    for token in tokens:
        chunk = {"choices": [{"delta": {"content": token}, "finish_reason": None}]}
        lines.append(b"data: " + json.dumps(chunk).encode() + b"\n")
    lines.append(b"data: [DONE]\n")
    return b"\n".join(lines)


def make_anthropic_stream(*tokens):
    """Build a fake Anthropic SSE stream from a list of token strings."""
    lines = []
    for token in tokens:
        event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": token}}
        lines.append(b"data: " + json.dumps(event).encode() + b"\n")
    lines.append(b'data: {"type":"message_stop"}\n')
    return b"\n".join(lines)


class FakeTTS:
    def __init__(self):
        self.spoken = []
    def speak(self, text):
        self.spoken.append(text)


class TestAIStreaming(unittest.TestCase):
    def test_openai_stream_dispatches_sentences(self):
        """Streaming OpenAI response dispatches completed sentences to TTS."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        fake_tts = FakeTTS()
        cfg = {
            "provider": "LM Studio",
            "endpoint": "http://localhost:1234/v1/chat/completions",
            "model": "test-model",
            "api_key": "",
            "system_prompt": "You are a test bot.",
            "tts_ai": True,
        }
        done = threading.Event()

        tokens = ["Hello", " world", ".", " How", " are", " you", "?"]
        stream_body = make_openai_stream(*tokens)

        mock_resp = MagicMock()
        mock_resp.iter_lines.return_value = iter(stream_body.split(b"\n"))
        mock_resp.raise_for_status = MagicMock()

        handler = twitch_bot.AIResponseHandler(
            get_config=lambda: cfg,
            log=lambda msg: None,
            tts=fake_tts,
        )

        with patch("requests.post", return_value=mock_resp):
            handler.handle("user", "hi", reply_cb=lambda _: done.set())
            done.wait(timeout=5)

        spoken = fake_tts.spoken
        self.assertGreater(len(spoken), 0)
        full = "".join(spoken)
        self.assertIn("Hello world.", full)

    def test_openai_stream_assembles_full_reply(self):
        """Full assembled text is passed to reply_cb."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        fake_tts = FakeTTS()
        cfg = {
            "provider": "LM Studio",
            "endpoint": "http://localhost:1234/v1/chat/completions",
            "model": "test-model",
            "api_key": "",
            "system_prompt": "You are a test bot.",
            "tts_ai": True,
        }
        replies = []
        done = threading.Event()

        tokens = ["Nice", " to", " meet", " you", "!"]
        stream_body = make_openai_stream(*tokens)

        mock_resp = MagicMock()
        mock_resp.iter_lines.return_value = iter(stream_body.split(b"\n"))
        mock_resp.raise_for_status = MagicMock()

        def cb(text):
            replies.append(text)
            done.set()

        handler = twitch_bot.AIResponseHandler(
            get_config=lambda: cfg,
            log=lambda msg: None,
            tts=fake_tts,
        )

        with patch("requests.post", return_value=mock_resp):
            handler.handle("user", "hello", reply_cb=cb)
            done.wait(timeout=5)

        self.assertEqual(len(replies), 1)
        self.assertIn("Nice to meet you!", replies[0])

    def test_anthropic_stream_dispatches_sentences(self):
        """Streaming Anthropic response dispatches sentences to TTS."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        fake_tts = FakeTTS()
        cfg = {
            "provider": "Claude",
            "endpoint": "https://api.anthropic.com/v1/messages",
            "model": "claude-opus-4-8",
            "api_key": "test-key",
            "system_prompt": "You are a test bot.",
            "tts_ai": True,
        }
        done = threading.Event()

        tokens = ["Hello", " there", ".", " Goodbye", "!"]
        stream_body = make_anthropic_stream(*tokens)

        mock_resp = MagicMock()
        mock_resp.iter_lines.return_value = iter(stream_body.split(b"\n"))
        mock_resp.raise_for_status = MagicMock()

        handler = twitch_bot.AIResponseHandler(
            get_config=lambda: cfg,
            log=lambda msg: None,
            tts=fake_tts,
        )

        with patch("requests.post", return_value=mock_resp):
            handler.handle("user", "hi", reply_cb=lambda _: done.set())
            done.wait(timeout=5)

        self.assertGreater(len(fake_tts.spoken), 0)


if __name__ == "__main__":
    unittest.main()
