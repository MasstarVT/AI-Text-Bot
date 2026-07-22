"""Tests for _ai_counter thread safety."""
import collections
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot
from unittest.mock import MagicMock


def _make_app(every_n: int = 2) -> twitch_bot.WebApp:
    """Construct a minimal WebApp stub for _route_ai counter tests."""
    app = object.__new__(twitch_bot.WebApp)
    app._config_lock      = threading.Lock()
    app._ai_counter       = 0
    app._ai_counter_lock  = threading.Lock()
    app._history_lock     = threading.Lock()
    app._chat_history     = collections.deque(maxlen=20)
    app._config = {
        "ai_enabled":         True,
        "trigger_every_n":    True,
        "every_n":            every_n,
        "trigger_mentions":   False,
        "trigger_bits":       False,
        "min_bits":           100,
        "trigger_points":     False,
        "reward_id":          "",
        "bot_username":       "bot",
        "ai_context_enabled": False,
        "ai_context_size":    5,
        "twitch_channel":     "ch",
    }
    app._ai  = MagicMock()
    app._irc = MagicMock()
    app._log = lambda m: None
    return app


class TestAICounterLockExists(unittest.TestCase):
    def test_ai_counter_lock_is_initialized(self):
        """WebApp must expose _ai_counter_lock as a real Lock."""
        app = _make_app()
        self.assertTrue(hasattr(app, "_ai_counter_lock"))
        acquired = app._ai_counter_lock.acquire(blocking=False)
        self.assertTrue(acquired, "_ai_counter_lock must be acquirable (not already held)")
        app._ai_counter_lock.release()


class TestAICounterSequential(unittest.TestCase):
    def test_counter_fires_correct_number_of_ai_calls(self):
        """N sequential _route_ai calls with every_n=2 must fire AI exactly N//2 times."""
        app = _make_app(every_n=2)
        n = 20
        for i in range(n):
            app._route_ai("viewer", f"msg{i}")
        actual = app._ai.handle.call_count
        self.assertEqual(
            actual, n // 2,
            f"Expected {n // 2} AI calls for {n} messages with every_n=2, got {actual}",
        )

    def test_counter_resets_after_trigger(self):
        """Counter must be 0 immediately after a trigger fires."""
        app = _make_app(every_n=3)
        for i in range(3):
            app._route_ai("viewer", f"msg{i}")
        self.assertEqual(
            app._ai_counter, 0,
            "Counter must reset to 0 after trigger fires",
        )

    def test_counter_increments_between_triggers(self):
        """Counter must sit at 1 after one message with every_n=3."""
        app = _make_app(every_n=3)
        app._route_ai("viewer", "msg0")
        self.assertEqual(app._ai_counter, 1)

    def test_every_n_one_fires_every_message(self):
        """every_n=1 must fire on every single message."""
        app = _make_app(every_n=1)
        n = 5
        for i in range(n):
            app._route_ai("viewer", f"msg{i}")
        self.assertEqual(app._ai.handle.call_count, n)
        self.assertEqual(app._ai_counter, 0)


if __name__ == "__main__":
    unittest.main()
