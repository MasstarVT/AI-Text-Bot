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


class TestRoleCommands(unittest.TestCase):
    def _app_with_dir(self, tmpdir: str) -> twitch_bot.WebApp:
        app = _make_app(data_dir=tmpdir)
        app._config["twitch_channel"] = "testchannel"
        return app

    def test_addrole_creates_role_and_member(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._app_with_dir(tmpdir)
            roles = {"moderator", "everyone"}
            app._route_role_commands("mod1", "!addrole viewer1 trusted", roles)
            with open(os.path.join(tmpdir, "roles.json")) as f:
                data = json.load(f)
            self.assertIn("viewer1", data.get("trusted", []))
            app._irc.say.assert_called_once()

    def test_addrole_requires_moderator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._app_with_dir(tmpdir)
            app._route_role_commands("viewer1", "!addrole viewer2 trusted", {"everyone"})
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "roles.json")))
            app._irc.say.assert_not_called()

    def test_removerole_removes_member(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "roles.json"), "w") as f:
                json.dump({"trusted": ["viewer1"]}, f)
            app = self._app_with_dir(tmpdir)
            app._route_role_commands("mod1", "!removerole viewer1 trusted", {"moderator", "everyone"})
            with open(os.path.join(tmpdir, "roles.json")) as f:
                data = json.load(f)
            self.assertNotIn("viewer1", data.get("trusted", []))

    def test_roles_command_lists_roles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "roles.json"), "w") as f:
                json.dump({"artist": ["viewer1"]}, f)
            app = self._app_with_dir(tmpdir)
            app._route_role_commands("mod1", "!roles viewer1", {"moderator", "everyone"})
            app._irc.say.assert_called_once()
            reply = app._irc.say.call_args[0][1]
            self.assertIn("artist", reply)

    def test_non_role_command_returns_false(self):
        app = _make_app()
        result = app._route_role_commands("viewer1", "!hello", {"everyone"})
        self.assertFalse(result)

    def test_role_command_returns_true(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._app_with_dir(tmpdir)
            result = app._route_role_commands("mod1", "!addrole viewer1 trusted", {"moderator", "everyone"})
            self.assertTrue(result)
