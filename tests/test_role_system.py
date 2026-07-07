"""Tests for _build_user_roles."""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


def _make_app(data_dir: str = "/nonexistent") -> twitch_bot.WebApp:
    app = object.__new__(twitch_bot.WebApp)
    app._config_lock   = threading.Lock()
    app._config        = {}
    app._roles_lock    = threading.Lock()
    app._counters_lock = threading.Lock()
    app._quotes_lock   = threading.Lock()
    app._data_dir      = data_dir
    app._irc           = MagicMock()
    app._log           = lambda msg: None
    return app


class TestBuildUserRoles(unittest.TestCase):
    def test_always_includes_everyone(self):
        app = _make_app()
        roles = app._build_user_roles("viewer1", "")
        self.assertIn("everyone", roles)

    def test_broadcaster_badge(self):
        app = _make_app()
        roles = app._build_user_roles("streamer", "broadcaster/1")
        self.assertIn("broadcaster", roles)

    def test_moderator_badge(self):
        app = _make_app()
        roles = app._build_user_roles("mod1", "moderator/1")
        self.assertIn("moderator", roles)

    def test_subscriber_badge(self):
        app = _make_app()
        roles = app._build_user_roles("sub1", "subscriber/3021")
        self.assertIn("subscriber", roles)

    def test_vip_badge(self):
        app = _make_app()
        roles = app._build_user_roles("vip1", "vip/1")
        self.assertIn("vip", roles)

    def test_multiple_badges(self):
        app = _make_app()
        roles = app._build_user_roles("modder", "moderator/1,subscriber/3021")
        self.assertIn("moderator", roles)
        self.assertIn("subscriber", roles)

    def test_custom_role_from_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "roles.json"), "w") as f:
                json.dump({"trusted": ["viewer1"]}, f)
            app = _make_app(data_dir=tmpdir)
            roles = app._build_user_roles("viewer1", "")
            self.assertIn("trusted", roles)

    def test_custom_role_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "roles.json"), "w") as f:
                json.dump({"artist": ["Viewer1"]}, f)
            app = _make_app(data_dir=tmpdir)
            roles = app._build_user_roles("viewer1", "")
            self.assertIn("artist", roles)

    def test_unknown_badge_ignored(self):
        app = _make_app()
        roles = app._build_user_roles("viewer1", "bits-leader/1")
        self.assertEqual(roles, {"everyone"})

    def test_missing_roles_file_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app(data_dir=tmpdir)
            roles = app._build_user_roles("viewer1", "")
            self.assertEqual(roles, {"everyone"})

    def test_corrupt_roles_file_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "roles.json"), "w") as f:
                f.write("not json")
            app = _make_app(data_dir=tmpdir)
            roles = app._build_user_roles("viewer1", "")
            self.assertEqual(roles, {"everyone"})
