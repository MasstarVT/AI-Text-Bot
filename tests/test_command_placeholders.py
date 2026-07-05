"""Unit tests for _apply_placeholders command response substitution."""
import unittest
import sys
import os

# twitch_bot.py lives one directory above tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from twitch_bot import _apply_placeholders


class TestApplyPlaceholders(unittest.TestCase):

    def test_user_placeholder(self):
        result = _apply_placeholders("Hello %user%!", "streamer", "mychannel", "!hi", "")
        self.assertEqual(result, "Hello streamer!")

    def test_channel_placeholder(self):
        result = _apply_placeholders("Welcome to %channel%!", "viewer", "mychannel", "!hi", "")
        self.assertEqual(result, "Welcome to mychannel!")

    def test_command_placeholder(self):
        result = _apply_placeholders("You used %command%", "viewer", "mychannel", "!so", "")
        self.assertEqual(result, "You used !so")

    def test_args_placeholder(self):
        result = _apply_placeholders("Go check out %args%!", "viewer", "mychannel", "!so", "@StreamerName")
        self.assertEqual(result, "Go check out @StreamerName!")

    def test_multiple_placeholders(self):
        result = _apply_placeholders(
            "%user% used %command% with args: %args% in %channel%",
            "viewer", "mychannel", "!so", "@friend"
        )
        self.assertEqual(result, "viewer used !so with args: @friend in mychannel")

    def test_unknown_placeholder_left_as_is(self):
        result = _apply_placeholders("Hello %unknown%!", "viewer", "mychannel", "!hi", "")
        self.assertEqual(result, "Hello %unknown%!")

    def test_no_placeholders(self):
        result = _apply_placeholders("Static response.", "viewer", "mychannel", "!hi", "")
        self.assertEqual(result, "Static response.")

    def test_empty_args(self):
        result = _apply_placeholders("Args: '%args%'", "viewer", "mychannel", "!cmd", "")
        self.assertEqual(result, "Args: ''")

    def test_repeated_placeholder(self):
        result = _apply_placeholders("%user% %user%", "alice", "ch", "!hi", "")
        self.assertEqual(result, "alice alice")


if __name__ == "__main__":
    unittest.main()
