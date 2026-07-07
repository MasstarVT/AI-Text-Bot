# tests/test_quotes.py
"""Tests for _route_quotes."""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


def _make_app(tmpdir: str, addquote_role: str = "moderator") -> twitch_bot.WebApp:
    app = object.__new__(twitch_bot.WebApp)
    app._config_lock  = threading.Lock()
    app._config       = {
        "twitch_channel":      "testchannel",
        "quote_addquote_role": addquote_role,
    }
    app._quotes_lock  = threading.Lock()
    app._data_dir     = tmpdir
    app._irc          = MagicMock()
    app._log          = lambda msg: None
    return app


def _write_quotes(tmpdir: str, quotes: list) -> None:
    with open(os.path.join(tmpdir, "quotes.json"), "w") as f:
        json.dump(quotes, f)


def _read_quotes(tmpdir: str) -> list:
    with open(os.path.join(tmpdir, "quotes.json")) as f:
        return json.load(f)


_MOD = {"moderator", "everyone"}
_ALL = {"everyone"}

_SAMPLE_QUOTES = [
    {"id": 1, "text": "First quote!", "author": "streamer", "added_by": "mod1", "timestamp": "2026-07-07T12:00:00"},
    {"id": 2, "text": "Second quote!", "author": "streamer", "added_by": "mod2", "timestamp": "2026-07-07T13:00:00"},
]


class TestRouteQuotes(unittest.TestCase):
    def test_random_quote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, _SAMPLE_QUOTES)
            app = _make_app(tmpdir)
            result = app._route_quotes("viewer1", "!quote", _ALL)
            self.assertTrue(result)
            app._irc.say.assert_called_once()
            _, reply = app._irc.say.call_args[0]
            self.assertIn("[#", reply)

    def test_quote_by_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, _SAMPLE_QUOTES)
            app = _make_app(tmpdir)
            app._route_quotes("viewer1", "!quote 2", _ALL)
            _, reply = app._irc.say.call_args[0]
            self.assertIn("Second quote!", reply)
            self.assertIn("[#2]", reply)

    def test_quote_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, _SAMPLE_QUOTES)
            app = _make_app(tmpdir)
            app._route_quotes("viewer1", "!quote 99", _ALL)
            _, reply = app._irc.say.call_args[0]
            self.assertIn("not found", reply.lower())

    def test_quotecount(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, _SAMPLE_QUOTES)
            app = _make_app(tmpdir)
            app._route_quotes("viewer1", "!quotecount", _ALL)
            _, reply = app._irc.say.call_args[0]
            self.assertIn("2", reply)

    def test_addquote_as_mod(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, [])
            app = _make_app(tmpdir)
            app._route_quotes("mod1", "!addquote This is a quote", _MOD)
            quotes = _read_quotes(tmpdir)
            self.assertEqual(len(quotes), 1)
            self.assertEqual(quotes[0]["text"], "This is a quote")
            self.assertEqual(quotes[0]["added_by"], "mod1")

    def test_addquote_blocked_for_viewer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, [])
            app = _make_app(tmpdir)
            app._route_quotes("viewer1", "!addquote Sneaky", _ALL)
            self.assertEqual(_read_quotes(tmpdir), [])

    def test_addquote_custom_role(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, [])
            app = _make_app(tmpdir, addquote_role="trusted")
            app._route_quotes("viewer1", "!addquote Nice quote", {"trusted", "everyone"})
            self.assertEqual(len(_read_quotes(tmpdir)), 1)

    def test_addquote_auto_increments_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, _SAMPLE_QUOTES)
            app = _make_app(tmpdir)
            app._route_quotes("mod1", "!addquote Third one", _MOD)
            quotes = _read_quotes(tmpdir)
            self.assertEqual(quotes[-1]["id"], 3)

    def test_delquote_as_mod(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, _SAMPLE_QUOTES)
            app = _make_app(tmpdir)
            app._route_quotes("mod1", "!delquote 1", _MOD)
            quotes = _read_quotes(tmpdir)
            ids = [q["id"] for q in quotes]
            self.assertNotIn(1, ids)

    def test_delquote_blocked_for_viewer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, _SAMPLE_QUOTES)
            app = _make_app(tmpdir)
            app._route_quotes("viewer1", "!delquote 1", _ALL)
            self.assertEqual(len(_read_quotes(tmpdir)), 2)

    def test_non_quote_command_returns_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, [])
            app = _make_app(tmpdir)
            result = app._route_quotes("viewer1", "!hello", _ALL)
            self.assertFalse(result)

    def test_empty_quotes_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, [])
            app = _make_app(tmpdir)
            app._route_quotes("viewer1", "!quote", _ALL)
            app._irc.say.assert_called_once()
            _, reply = app._irc.say.call_args[0]
            self.assertIn("No quotes", reply)
