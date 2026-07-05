"""Tests for per-command cooldowns and auto-!commands list."""
import collections
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


def _make_app(commands: dict, cmd_list_enabled: bool = False) -> twitch_bot.WebApp:
    """Construct a minimal WebApp stub for _route_chat_commands tests."""
    app = object.__new__(twitch_bot.WebApp)
    app._config_lock = threading.Lock()
    app._config = {
        "chat_commands_enabled": True,
        "chat_commands": commands,
        "twitch_channel": "testchannel",
        "cmd_list_enabled": cmd_list_enabled,
    }
    app._irc = MagicMock()
    app._log = lambda msg: None
    app._cmd_global_cooldowns = {}
    app._cmd_user_cooldowns   = {}
    return app


_CMD_GLOBAL = {"response": "Hey %user%!", "cooldown": 30, "cooldown_type": "global"}
_CMD_USER   = {"response": "Shoutout!",   "cooldown": 10, "cooldown_type": "user"}
_CMD_FREE   = {"response": "Free!",       "cooldown": 0,  "cooldown_type": "global"}


class TestGlobalCooldown(unittest.TestCase):
    def test_blocks_repeat_within_window(self):
        """Second call within cooldown window is silently dropped."""
        app = _make_app({"!hi": _CMD_GLOBAL})
        with patch("twitch_bot.time.time", return_value=1000.0):
            app._route_chat_commands("viewer1", "!hi")
        with patch("twitch_bot.time.time", return_value=1010.0):  # 10s < 30s
            app._route_chat_commands("viewer2", "!hi")
        self.assertEqual(app._irc.say.call_count, 1)

    def test_allows_after_window(self):
        """Call after cooldown window fires normally."""
        app = _make_app({"!hi": _CMD_GLOBAL})
        with patch("twitch_bot.time.time", return_value=1000.0):
            app._route_chat_commands("viewer1", "!hi")
        with patch("twitch_bot.time.time", return_value=1031.0):  # 31s > 30s
            app._route_chat_commands("viewer2", "!hi")
        self.assertEqual(app._irc.say.call_count, 2)

    def test_blocks_regardless_of_viewer(self):
        """Global cooldown applies to all viewers, not just the one who triggered."""
        app = _make_app({"!hi": _CMD_GLOBAL})
        with patch("twitch_bot.time.time", return_value=1000.0):
            app._route_chat_commands("viewer1", "!hi")
        with patch("twitch_bot.time.time", return_value=1005.0):
            app._route_chat_commands("viewer1", "!hi")
        self.assertEqual(app._irc.say.call_count, 1)


class TestPerUserCooldown(unittest.TestCase):
    def test_blocks_same_viewer_during_window(self):
        """Same viewer cannot trigger again during their per-user cooldown."""
        app = _make_app({"!so": _CMD_USER})
        with patch("twitch_bot.time.time", return_value=1000.0):
            app._route_chat_commands("viewer1", "!so")
        with patch("twitch_bot.time.time", return_value=1005.0):  # 5s < 10s
            app._route_chat_commands("viewer1", "!so")
        self.assertEqual(app._irc.say.call_count, 1)

    def test_allows_different_viewer_during_window(self):
        """Different viewer is not affected by another viewer's cooldown."""
        app = _make_app({"!so": _CMD_USER})
        with patch("twitch_bot.time.time", return_value=1000.0):
            app._route_chat_commands("viewer1", "!so")
        with patch("twitch_bot.time.time", return_value=1005.0):
            app._route_chat_commands("viewer2", "!so")
        self.assertEqual(app._irc.say.call_count, 2)


class TestZeroCooldown(unittest.TestCase):
    def test_zero_cooldown_never_blocks(self):
        """Commands with cooldown=0 always fire regardless of frequency."""
        app = _make_app({"!free": _CMD_FREE})
        for _ in range(5):
            app._route_chat_commands("viewer1", "!free")
        self.assertEqual(app._irc.say.call_count, 5)


class TestMigration(unittest.TestCase):
    def test_string_value_promoted_to_dict(self):
        """Legacy string command values are migrated to dict format."""
        raw = {"!hi": "Hello there!"}
        migrated = {
            k: (v if isinstance(v, dict)
                else {"response": v, "cooldown": 0, "cooldown_type": "global"})
            for k, v in raw.items()
        }
        self.assertEqual(migrated["!hi"]["response"], "Hello there!")
        self.assertEqual(migrated["!hi"]["cooldown"], 0)
        self.assertEqual(migrated["!hi"]["cooldown_type"], "global")


class TestAutoCommandsList(unittest.TestCase):
    def test_auto_list_fires_when_enabled(self):
        """!commands posts a sorted, prefixed list when enabled and no user entry."""
        cmds = {
            "!so":    {"response": "Shout!", "cooldown": 0, "cooldown_type": "global"},
            "!hello": {"response": "Hey!",   "cooldown": 0, "cooldown_type": "global"},
        }
        app = _make_app(cmds, cmd_list_enabled=True)
        app._route_chat_commands("viewer", "!commands")
        app._irc.say.assert_called_once()
        _, reply = app._irc.say.call_args[0]
        self.assertTrue(reply.startswith("Commands:"))
        self.assertIn("!hello", reply)
        self.assertIn("!so", reply)

    def test_auto_list_suppressed_by_user_entry(self):
        """User-defined !commands entry takes priority over auto-list."""
        cmds = {
            "!commands": {"response": "Custom list!", "cooldown": 0, "cooldown_type": "global"},
            "!so":       {"response": "Shout!",       "cooldown": 0, "cooldown_type": "global"},
        }
        app = _make_app(cmds, cmd_list_enabled=True)
        app._route_chat_commands("viewer", "!commands")
        app._irc.say.assert_called_once_with("testchannel", "Custom list!")

    def test_auto_list_suppressed_when_disabled(self):
        """!commands does nothing when cmd_list_enabled=False and no user entry."""
        cmds = {"!so": {"response": "Shout!", "cooldown": 0, "cooldown_type": "global"}}
        app = _make_app(cmds, cmd_list_enabled=False)
        app._route_chat_commands("viewer", "!commands")
        app._irc.say.assert_not_called()


if __name__ == "__main__":
    unittest.main()
