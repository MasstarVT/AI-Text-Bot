# tests/test_role_gating.py
"""Tests for allowed_roles gating on _route_chat_commands."""
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


def _make_app(commands: dict) -> twitch_bot.WebApp:
    app = object.__new__(twitch_bot.WebApp)
    app._config_lock = threading.Lock()
    app._config = {
        "chat_commands_enabled": True,
        "chat_commands": commands,
        "twitch_channel": "testchannel",
        "cmd_list_enabled": False,
    }
    app._irc = MagicMock()
    app._log = lambda msg: None
    app._cmd_global_cooldowns = {}
    app._cmd_user_cooldowns   = {}
    app._cmd_use_counts       = {}
    app._data_dir             = os.path.join(os.path.dirname(__file__), "..", "data")
    app._stream_cache         = {}
    app._stream_cache_ts      = 0.0
    return app


_CMD_MOD_ONLY = {"response": "Mod response!", "cooldown": 0,
                 "cooldown_type": "global", "allowed_roles": ["moderator"]}
_CMD_SUB_ONLY = {"response": "Sub response!", "cooldown": 0,
                 "cooldown_type": "global", "allowed_roles": ["subscriber"]}
_CMD_OPEN     = {"response": "Open!",         "cooldown": 0,
                 "cooldown_type": "global", "allowed_roles": []}


class TestCommandRoleGating(unittest.TestCase):
    def test_mod_only_fires_for_mod(self):
        app = _make_app({"!secret": _CMD_MOD_ONLY})
        app._route_chat_commands("mod1", "!secret", {"moderator", "everyone"})
        app._irc.say.assert_called_once()

    def test_mod_only_blocked_for_viewer(self):
        app = _make_app({"!secret": _CMD_MOD_ONLY})
        app._route_chat_commands("viewer1", "!secret", {"everyone"})
        app._irc.say.assert_not_called()

    def test_mod_only_fires_for_broadcaster(self):
        app = _make_app({"!secret": _CMD_MOD_ONLY})
        app._route_chat_commands("streamer", "!secret", {"broadcaster", "everyone"})
        app._irc.say.assert_not_called()  # broadcaster not in allowed_roles: ["moderator"]

    def test_broadcaster_in_allowed_roles_fires(self):
        cmd = {"response": "hi", "cooldown": 0, "cooldown_type": "global",
               "allowed_roles": ["broadcaster"]}
        app = _make_app({"!cmd": cmd})
        app._route_chat_commands("streamer", "!cmd", {"broadcaster", "everyone"})
        app._irc.say.assert_called_once()

    def test_open_command_fires_for_everyone(self):
        app = _make_app({"!hello": _CMD_OPEN})
        app._route_chat_commands("viewer1", "!hello", {"everyone"})
        app._irc.say.assert_called_once()

    def test_no_user_roles_bypasses_check(self):
        """user_roles=None means old callers — no gating applied."""
        app = _make_app({"!secret": _CMD_MOD_ONLY})
        app._route_chat_commands("viewer1", "!secret", None)
        app._irc.say.assert_called_once()

    def test_sub_command_fires_for_sub(self):
        app = _make_app({"!sub": _CMD_SUB_ONLY})
        app._route_chat_commands("sub1", "!sub", {"subscriber", "everyone"})
        app._irc.say.assert_called_once()

    def test_sub_command_blocked_for_non_sub(self):
        app = _make_app({"!sub": _CMD_SUB_ONLY})
        app._route_chat_commands("viewer1", "!sub", {"everyone"})
        app._irc.say.assert_not_called()

    def test_custom_role_gates_command(self):
        cmd = {"response": "hi", "cooldown": 0, "cooldown_type": "global",
               "allowed_roles": ["trusted"]}
        app = _make_app({"!vip": cmd})
        app._route_chat_commands("viewer1", "!vip", {"trusted", "everyone"})
        app._irc.say.assert_called_once()


if __name__ == "__main__":
    unittest.main()
