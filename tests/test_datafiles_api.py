"""Tests for /api/datafiles Flask routes."""
import json
import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestDataFilesAPI(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        import twitch_bot
        import threading
        import flask as _flask
        from unittest.mock import patch, MagicMock

        app_obj = twitch_bot.WebApp.__new__(twitch_bot.WebApp)
        app_obj._here              = self.tmp
        app_obj._data_dir          = os.path.join(self.tmp, "data")
        app_obj._prompts_dir       = os.path.join(self.tmp, "prompts")
        app_obj._presets_dir       = os.path.join(self.tmp, "presets")
        app_obj._config_lock       = threading.Lock()
        app_obj._config            = {}
        app_obj._stream_cache      = {}
        app_obj._stream_cache_ts   = 0.0
        app_obj._cmd_use_counts    = {}
        app_obj._cmd_global_cooldowns = {}
        app_obj._cmd_user_cooldowns   = {}
        app_obj._flask = _flask.Flask(__name__)

        # Minimal _log stub so routes can call self._log without crashing
        app_obj._log = lambda msg: None

        with patch.object(twitch_bot.WebApp, '_register_routes', lambda self: None):
            pass
        app_obj._register_routes()

        self.client = app_obj._flask.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_file(self, name, content="hello"):
        data_dir = os.path.join(self.tmp, "data")
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, name), "w") as f:
            f.write(content)

    # ── GET /api/datafiles ────────────────────────────────────────────────────

    def test_list_empty(self):
        resp = self.client.get("/api/datafiles")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["files"], [])

    def test_list_returns_files(self):
        self._make_file("deaths.txt", "0")
        resp = self.client.get("/api/datafiles")
        files = resp.get_json()["files"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["name"], "deaths.txt")
        self.assertIn("size", files[0])

    # ── GET /api/datafiles/<name> ─────────────────────────────────────────────

    def test_read_existing_file(self):
        self._make_file("facts.txt", "Cats sleep 16h a day")
        resp = self.client.get("/api/datafiles/facts.txt")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["content"], "Cats sleep 16h a day")

    def test_read_missing_file(self):
        resp = self.client.get("/api/datafiles/missing.txt")
        self.assertEqual(resp.status_code, 404)

    def test_read_invalid_name(self):
        resp = self.client.get("/api/datafiles/..%2Fetc%2Fpasswd")
        self.assertIn(resp.status_code, [400, 404])

    # ── POST /api/datafiles/<name> ────────────────────────────────────────────

    def test_create_new_file(self):
        resp = self.client.post(
            "/api/datafiles/new.txt",
            data=json.dumps({"content": "line1\nline2"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        path = os.path.join(self.tmp, "data", "new.txt")
        with open(path) as f:
            self.assertEqual(f.read(), "line1\nline2")

    def test_overwrite_existing_file(self):
        self._make_file("deaths.txt", "5")
        self.client.post(
            "/api/datafiles/deaths.txt",
            data=json.dumps({"content": "10"}),
            content_type="application/json",
        )
        path = os.path.join(self.tmp, "data", "deaths.txt")
        with open(path) as f:
            self.assertEqual(f.read(), "10")

    def test_create_invalid_name(self):
        resp = self.client.post(
            "/api/datafiles/..%2Fsecret",
            data=json.dumps({"content": "x"}),
            content_type="application/json",
        )
        self.assertIn(resp.status_code, [400, 404])

    # ── DELETE /api/datafiles/<name> ──────────────────────────────────────────

    def test_delete_existing_file(self):
        self._make_file("old.txt")
        resp = self.client.delete("/api/datafiles/old.txt")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "data", "old.txt")))

    def test_delete_missing_file(self):
        resp = self.client.delete("/api/datafiles/missing.txt")
        self.assertEqual(resp.status_code, 404)

    def test_delete_invalid_name(self):
        resp = self.client.delete("/api/datafiles/..%2Fetc%2Fpasswd")
        self.assertIn(resp.status_code, [400, 404])


if __name__ == "__main__":
    unittest.main()
