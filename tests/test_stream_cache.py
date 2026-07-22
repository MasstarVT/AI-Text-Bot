# tests/test_stream_cache.py
"""Tests for _fetch_stream_info thread safety and caching."""
import os, sys, threading, time, unittest
from unittest.mock import patch, MagicMock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


def _make_app():
    app = object.__new__(twitch_bot.WebApp)
    app._config_lock        = threading.Lock()
    app._stream_cache_lock  = threading.Lock()
    app._stream_cache       = {}
    app._stream_cache_ts    = 0.0  # force stale
    app._config = {
        "twitch_channel":   "testchannel",
        "twitch_token":     "oauth:abc",
        "twitch_client_id": "cid123",
    }
    app._log = lambda m: None
    return app


class TestStreamCacheLockExists(unittest.TestCase):
    def test_stream_cache_lock_is_initialized(self):
        app = _make_app()
        acquired = app._stream_cache_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        app._stream_cache_lock.release()


class TestStreamCacheFreshness(unittest.TestCase):
    def test_fresh_cache_returned_without_http(self):
        """A cache younger than 60s must be returned without making an HTTP request."""
        app = _make_app()
        app._stream_cache    = {"game_name": "Chess"}
        app._stream_cache_ts = time.time()  # fresh

        with patch.object(twitch_bot.requests, "get") as mock_get:
            result = app._fetch_stream_info()

        mock_get.assert_not_called()
        self.assertEqual(result.get("game_name"), "Chess")

    def test_stale_cache_triggers_http_request(self):
        """A stale cache must trigger an HTTP request."""
        app = _make_app()
        app._stream_cache_ts = 0.0  # stale

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"data": [{"game_name": "Minecraft", "title": "test", "viewer_count": 10}]}

        with patch.object(twitch_bot.requests, "get", return_value=mock_resp):
            result = app._fetch_stream_info()

        self.assertEqual(result.get("game_name"), "Minecraft")


class TestStreamCacheDoubleRequest(unittest.TestCase):
    def test_concurrent_stale_cache_fires_at_most_one_http_request(self):
        """4 concurrent threads hitting a stale cache must send at most 1 HTTP request."""
        app = _make_app()
        request_count = [0]
        count_lock = threading.Lock()

        def fake_get(url, **kwargs):
            with count_lock:
                request_count[0] += 1
            time.sleep(0.05)  # simulate network latency
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = {"data": [{"game_name": "Chess"}]}
            return resp

        barrier = threading.Barrier(4)

        # Patch at module level once, before spawning threads
        with patch.object(twitch_bot.requests, "get", side_effect=fake_get):
            def call():
                barrier.wait()
                app._fetch_stream_info()

            threads = [threading.Thread(target=call) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(request_count[0], 1,
            f"Expected 1 HTTP request, got {request_count[0]}")


if __name__ == "__main__":
    unittest.main()
