"""Unit tests for TwitchIRCClient exponential backoff reconnect."""
import threading
import time
import unittest
from unittest.mock import patch


class TestTwitchReconnectBackoff(unittest.TestCase):
    def _make_client(self, on_reconnecting=None):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        creds = {"channel": "testchan", "username": "testbot", "token": "oauth:abc"}
        client = twitch_bot.TwitchIRCClient(
            get_creds=lambda: creds,
            log=lambda msg: None,
            on_message=lambda *a: None,
            on_reconnecting=on_reconnecting,
        )
        return client

    def test_on_reconnecting_called_on_drop(self):
        """on_reconnecting callback is called when the session drops unexpectedly."""
        reconnect_calls = []
        done = threading.Event()

        def on_reconnecting():
            reconnect_calls.append(1)
            done.set()

        client = self._make_client(on_reconnecting=on_reconnecting)

        call_count = [0]
        thread_done = threading.Event()

        def fake_session():
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionResetError("server dropped")
            client._running = False  # stop after second attempt
            thread_done.set()

        client._session = fake_session

        with patch("time.sleep"):
            client.connect()
            done.wait(timeout=5)
            thread_done.wait(timeout=5)

        self.assertGreaterEqual(len(reconnect_calls), 1)

    def test_backoff_delay_increases(self):
        """Reconnect delay doubles on each failure up to 30s cap."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        delays_slept = []
        client = self._make_client()

        call_count = [0]
        thread_done = threading.Event()

        def fake_session():
            call_count[0] += 1
            if call_count[0] < 4:
                raise ConnectionResetError("dropped")
            client._running = False
            thread_done.set()

        client._session = fake_session

        def fake_sleep(n):
            delays_slept.append(n)

        with patch("time.sleep", side_effect=fake_sleep):
            client.connect()
            thread_done.wait(timeout=5)

        # Delays should be 1, 2, 4 (doubling) for the three failures
        self.assertEqual(delays_slept[:3], [1, 2, 4])
