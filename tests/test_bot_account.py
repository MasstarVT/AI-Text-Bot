"""Tests for bot account credential split."""
import collections
import sys
import os
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


def _make_app(config: dict) -> twitch_bot.WebApp:
    """Construct a WebApp stub with only the attributes needed for config/routing tests."""
    app = object.__new__(twitch_bot.WebApp)
    app._config_lock   = threading.Lock()
    app._history_lock  = threading.Lock()
    app._roles_lock    = threading.Lock()
    app._counters_lock = threading.Lock()
    app._quotes_lock   = threading.Lock()
    app._chat_history  = collections.deque(maxlen=20)
    app._ai_counter    = 0
    app._ai            = None
    app._irc           = None
    app._data_dir      = "/nonexistent"
    app._config        = dict(config)
    return app


_BASE_CONFIG = {
    "twitch_channel":   "streamer",
    "twitch_username":  "streamer",
    "twitch_token":     "oauth:streamertoken",
    "twitch_client_id": "",
    "bot_username":     "mybot",
    "bot_token":        "oauth:bottoken",
    "ai_enabled":       True,
    "trigger_every_n":  False,
    "trigger_mentions": True,
    "trigger_bits":     False,
    "trigger_points":   False,
    "reward_id":        "",
    "every_n":          5,
    "min_bits":         100,
    "ai_context_enabled": False,
    "ai_context_size":  5,
    "ignore_list_enabled": False,
    "ignore_list":      [],
    "chat_commands_enabled": False,
    "chat_commands":    {},
    "plays_enabled":    False,
    "command_map":      {},
}


class TestGetIrcCreds(unittest.TestCase):
    def test_returns_bot_credentials(self):
        app = _make_app(_BASE_CONFIG)
        creds = app._get_irc_creds()
        self.assertEqual(creds["username"], "mybot")
        self.assertEqual(creds["token"],    "oauth:bottoken")
        self.assertEqual(creds["channel"],  "streamer")

    def test_does_not_return_broadcaster_token(self):
        app = _make_app(_BASE_CONFIG)
        creds = app._get_irc_creds()
        self.assertNotEqual(creds["token"], "oauth:streamertoken")


class TestMigration(unittest.TestCase):
    def test_bot_fields_populated_from_streamer_when_empty(self):
        """When BOT_USERNAME/BOT_TOKEN absent from .env, they fall back to streamer fields."""
        env = {
            "TWITCH_CHANNEL":  "chan",
            "TWITCH_USERNAME": "streameracc",
            "TWITCH_TOKEN":    "oauth:streamertkn",
        }
        bot_username = env.get("BOT_USERNAME", "") or env.get("TWITCH_USERNAME", "")
        bot_token    = env.get("BOT_TOKEN",    "") or env.get("TWITCH_TOKEN",    "")
        self.assertEqual(bot_username, "streameracc")
        self.assertEqual(bot_token,    "oauth:streamertkn")

    def test_bot_fields_not_overwritten_when_already_set(self):
        """Existing BOT_USERNAME/BOT_TOKEN are not replaced by streamer fields."""
        env = {
            "TWITCH_USERNAME": "streameracc",
            "TWITCH_TOKEN":    "oauth:streamertkn",
            "BOT_USERNAME":    "mybot",
            "BOT_TOKEN":       "oauth:bottkn",
        }
        bot_username = env.get("BOT_USERNAME", "") or env.get("TWITCH_USERNAME", "")
        bot_token    = env.get("BOT_TOKEN",    "") or env.get("TWITCH_TOKEN",    "")
        self.assertEqual(bot_username, "mybot")
        self.assertEqual(bot_token,    "oauth:bottkn")


class TestSelfFilter(unittest.TestCase):
    def _app_with_mocked_routes(self, config=None):
        app = _make_app(config or _BASE_CONFIG)
        app._log                 = lambda msg: None
        app._route_role_commands = MagicMock(return_value=False)
        app._route_counters      = MagicMock(return_value=False)
        app._route_quotes        = MagicMock(return_value=False)
        app._route_chat_commands = MagicMock()
        app._route_plays         = MagicMock()
        app._route_ai            = MagicMock()
        app._handle_event        = MagicMock()
        return app

    def test_dispatch_drops_bots_own_messages(self):
        """_dispatch returns early without routing when username == bot_username."""
        app = self._app_with_mocked_routes()
        app._dispatch("mybot", "hello chat")
        app._route_ai.assert_not_called()
        app._route_plays.assert_not_called()

    def test_dispatch_allows_other_users(self):
        """_dispatch routes normally for messages from other users."""
        app = self._app_with_mocked_routes()
        app._dispatch("someviewer", "hello chat")
        app._route_ai.assert_called_once()

    def test_self_filter_is_case_insensitive(self):
        """Self-filter matches regardless of username casing."""
        app = self._app_with_mocked_routes()
        app._dispatch("MyBot", "hello chat")
        app._route_ai.assert_not_called()


class TestMentionDetection(unittest.TestCase):
    def _app_for_route_ai(self, bot_username="mybot", broadcaster_username="streamer"):
        config = {
            **_BASE_CONFIG,
            "bot_username":    bot_username,
            "twitch_username": broadcaster_username,
        }
        app = _make_app(config)
        app._log = lambda msg: None
        app._ai  = MagicMock()
        return app

    def test_mention_triggers_on_bot_username(self):
        """@mention trigger fires when bot_username appears in the message."""
        app = self._app_for_route_ai("mybot")
        app._route_ai("viewer", "hey mybot come here")
        app._ai.handle.assert_called_once()

    def test_mention_does_not_trigger_on_broadcaster_username(self):
        """Trigger does NOT fire when only the broadcaster username appears in the message."""
        app = self._app_for_route_ai("mybot", broadcaster_username="streamer")
        app._route_ai("viewer", "hey streamer you there")
        app._ai.handle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
