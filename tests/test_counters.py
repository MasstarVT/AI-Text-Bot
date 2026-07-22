# tests/test_counters.py
"""Tests for _route_counters."""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


def _make_app(tmpdir: str) -> twitch_bot.WebApp:
    app = object.__new__(twitch_bot.WebApp)
    app._config_lock   = threading.Lock()
    app._config        = {"twitch_channel": "testchannel"}
    app._counters_lock = threading.Lock()
    app._data_dir      = tmpdir
    app._irc           = MagicMock()
    app._log           = lambda msg: None
    return app


def _write_counters(tmpdir: str, data: dict) -> None:
    with open(os.path.join(tmpdir, "counters.json"), "w") as f:
        json.dump(data, f)


def _read_counters(tmpdir: str) -> dict:
    with open(os.path.join(tmpdir, "counters.json")) as f:
        return json.load(f)


_MOD = {"moderator", "everyone"}
_ALL = {"everyone"}

_BASE_COUNTERS = {
    "deaths": {"value": 5, "display": "Deaths: {value}", "edit_roles": ["moderator"]}
}


class TestRouteCounters(unittest.TestCase):
    def test_display_counter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, _BASE_COUNTERS)
            app = _make_app(tmpdir)
            result = app._route_counters("viewer1", "!deaths", _ALL)
            self.assertTrue(result)
            app._irc.say.assert_called_once()
            _, reply = app._irc.say.call_args[0]
            self.assertIn("5", reply)

    def test_increment_as_mod(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, _BASE_COUNTERS)
            app = _make_app(tmpdir)
            app._route_counters("mod1", "!deaths +1", _MOD)
            self.assertEqual(_read_counters(tmpdir)["deaths"]["value"], 6)

    def test_increment_blocked_for_viewer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, _BASE_COUNTERS)
            app = _make_app(tmpdir)
            app._route_counters("viewer1", "!deaths +1", _ALL)
            self.assertEqual(_read_counters(tmpdir)["deaths"]["value"], 5)

    def test_decrement_floors_at_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            counters = {"deaths": {"value": 0, "display": "Deaths: {value}", "edit_roles": ["moderator"]}}
            _write_counters(tmpdir, counters)
            app = _make_app(tmpdir)
            app._route_counters("mod1", "!deaths -1", _MOD)
            self.assertEqual(_read_counters(tmpdir)["deaths"]["value"], 0)

    def test_set_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, _BASE_COUNTERS)
            app = _make_app(tmpdir)
            app._route_counters("mod1", "!deaths set 42", _MOD)
            self.assertEqual(_read_counters(tmpdir)["deaths"]["value"], 42)

    def test_reset_counter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, _BASE_COUNTERS)
            app = _make_app(tmpdir)
            app._route_counters("mod1", "!deaths reset", _MOD)
            self.assertEqual(_read_counters(tmpdir)["deaths"]["value"], 0)

    def test_addcounter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, {})
            app = _make_app(tmpdir)
            app._route_counters("mod1", "!addcounter hype", _MOD)
            self.assertIn("hype", _read_counters(tmpdir))

    def test_delcounter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, _BASE_COUNTERS)
            app = _make_app(tmpdir)
            app._route_counters("mod1", "!delcounter deaths", _MOD)
            self.assertNotIn("deaths", _read_counters(tmpdir))

    def test_unknown_counter_returns_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, {})
            app = _make_app(tmpdir)
            result = app._route_counters("viewer1", "!deaths", _ALL)
            self.assertFalse(result)

    def test_non_counter_message_returns_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, _BASE_COUNTERS)
            app = _make_app(tmpdir)
            result = app._route_counters("viewer1", "hello world", _ALL)
            self.assertFalse(result)

    def test_display_format_applied(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            counters = {"deaths": {"value": 3, "display": "💀 {value} deaths!", "edit_roles": []}}
            _write_counters(tmpdir, counters)
            app = _make_app(tmpdir)
            app._route_counters("viewer1", "!deaths", _ALL)
            _, reply = app._irc.say.call_args[0]
            self.assertEqual(reply, "💀 3 deaths!")


class TestCounterSetNegative(unittest.TestCase):
    def test_set_negative_is_clamped_to_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, {"deaths": {"value": 5, "display": "Deaths: {value}", "edit_roles": ["moderator"]}})
            app = _make_app(tmpdir)
            app._route_counters("broadcaster", "!deaths set -5", {"broadcaster", "everyone"})
            data = _read_counters(tmpdir)
            self.assertGreaterEqual(data["deaths"]["value"], 0,
                "Counter value must not go negative via 'set' command")

    def test_set_zero_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, {"deaths": {"value": 5, "display": "Deaths: {value}", "edit_roles": ["moderator"]}})
            app = _make_app(tmpdir)
            app._route_counters("broadcaster", "!deaths set 0", {"broadcaster", "everyone"})
            data = _read_counters(tmpdir)
            self.assertEqual(data["deaths"]["value"], 0)


class TestAddcounterPermissionFeedback(unittest.TestCase):
    def test_addcounter_no_permission_returns_false(self):
        """Non-mod !addcounter should return False so fallthrough works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, {})
            app = _make_app(tmpdir)
            result = app._route_counters("viewer", "!addcounter foo", {"everyone"})
            self.assertFalse(result, "Unauthorized !addcounter must return False to allow fallthrough")

    def test_delcounter_no_permission_returns_false(self):
        """Non-mod !delcounter should return False so fallthrough works."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, {"deaths": {"value": 0, "display": "Deaths: {value}", "edit_roles": ["moderator"]}})
            app = _make_app(tmpdir)
            result = app._route_counters("viewer", "!delcounter deaths", {"everyone"})
            self.assertFalse(result, "Unauthorized !delcounter must return False to allow fallthrough")
