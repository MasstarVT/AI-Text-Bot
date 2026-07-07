"""Unit tests for _apply_placeholders command response substitution."""
import unittest
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from twitch_bot import _apply_placeholders


def _ph(response, username="viewer", channel="mychannel", command="!cmd", args="",
         cmd_count=0, stream_info=None, data_dir=""):
    """Convenience wrapper with defaults for new params."""
    return _apply_placeholders(response, username, channel, command, args,
                                cmd_count, stream_info, data_dir)


class TestLocalPlaceholders(unittest.TestCase):

    # ── existing ──────────────────────────────────────────────────────────────

    def test_user(self):
        self.assertEqual(_ph("Hello %user%!", "streamer"), "Hello streamer!")

    def test_channel(self):
        self.assertEqual(_ph("Welcome to %channel%!"), "Welcome to mychannel!")

    def test_command(self):
        self.assertEqual(_ph("You used %command%", command="!so"), "You used !so")

    def test_args(self):
        self.assertEqual(_ph("Go check out %args%!", args="@StreamerName"),
                         "Go check out @StreamerName!")

    def test_multiple(self):
        result = _ph("%user% used %command% with args: %args% in %channel%",
                     username="viewer", channel="mychannel", command="!so", args="@friend")
        self.assertEqual(result, "viewer used !so with args: @friend in mychannel")

    def test_unknown_left_as_is(self):
        self.assertEqual(_ph("Hello %unknown%!"), "Hello %unknown%!")

    def test_no_placeholders(self):
        self.assertEqual(_ph("Static response."), "Static response.")

    def test_empty_args(self):
        self.assertEqual(_ph("Args: '%args%'", args=""), "Args: ''")

    def test_repeated(self):
        self.assertEqual(_ph("%user% %user%", username="alice"), "alice alice")

    # ── new local ─────────────────────────────────────────────────────────────

    def test_touser_strips_at(self):
        self.assertEqual(_ph("%touser%", args="@StreamerName"), "StreamerName")

    def test_touser_no_at(self):
        self.assertEqual(_ph("%touser%", args="StreamerName"), "StreamerName")

    def test_touser_takes_first_word(self):
        self.assertEqual(_ph("%touser%", args="@Alice extra text"), "Alice")

    def test_touser_empty_args(self):
        self.assertEqual(_ph("%touser%", args=""), "")

    def test_touser_whitespace_only_args(self):
        self.assertEqual(_ph("%touser%", args="   "), "")

    def test_time_format(self):
        result = _ph("%time%")
        self.assertRegex(result, r"^\d{2}:\d{2}$")

    def test_date_format(self):
        result = _ph("%date%")
        # e.g. "July 6, 2026"
        self.assertRegex(result, r"^[A-Z][a-z]+ \d+, \d{4}$")

    def test_count(self):
        self.assertEqual(_ph("%count%", cmd_count=7), "7")

    def test_count_zero(self):
        self.assertEqual(_ph("%count%", cmd_count=0), "0")

    def test_random_default_range(self):
        for _ in range(20):
            v = int(_ph("%random%"))
            self.assertGreaterEqual(v, 1)
            self.assertLessEqual(v, 100)

    def test_random_custom_range(self):
        for _ in range(20):
            v = int(_ph("%random:5-10%"))
            self.assertGreaterEqual(v, 5)
            self.assertLessEqual(v, 10)

    def test_random_single_value_range(self):
        self.assertEqual(_ph("%random:42-42%"), "42")

    def test_random_invalid_range_left_as_is(self):
        result = _ph("%random:100-1%")
        self.assertEqual(result, "%random:100-1%")


class TestAPIPlaceholders(unittest.TestCase):

    def test_game_from_stream_info(self):
        self.assertEqual(_ph("%game%", stream_info={"game_name": "Minecraft"}), "Minecraft")

    def test_game_offline_when_empty(self):
        self.assertEqual(_ph("%game%", stream_info={}), "offline")

    def test_game_offline_when_none(self):
        self.assertEqual(_ph("%game%", stream_info=None), "offline")

    def test_title(self):
        self.assertEqual(_ph("%title%", stream_info={"title": "Playing games!"}), "Playing games!")

    def test_viewers(self):
        self.assertEqual(_ph("%viewers%", stream_info={"viewer_count": 42}), "42")

    def test_uptime_hours_and_minutes(self):
        from datetime import timezone, timedelta
        from datetime import datetime as dt
        started = (dt.now(timezone.utc) - timedelta(hours=2, minutes=14)).isoformat()
        result = _ph("%uptime%", stream_info={"started_at": started})
        self.assertIn("2h", result)
        self.assertIn("14m", result)

    def test_uptime_minutes_only(self):
        from datetime import timezone, timedelta
        from datetime import datetime as dt
        started = (dt.now(timezone.utc) - timedelta(minutes=37)).isoformat()
        result = _ph("%uptime%", stream_info={"started_at": started})
        self.assertNotIn("0h", result)
        self.assertIn("37m", result)

    def test_uptime_offline_when_no_started_at(self):
        self.assertEqual(_ph("%uptime%", stream_info={}), "offline")


if __name__ == "__main__":
    unittest.main()
