# tests/test_atomic_writes.py
"""
Verify that save functions write atomically using tmp+rename.
"""
import json, os, sys, tempfile, threading, unittest
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


class TestAtomicSettingsSave(unittest.TestCase):
    def test_crash_during_save_leaves_old_file_intact(self):
        """If json.dump raises, the original settings.json must be untouched."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            original = {"every_n": 5, "min_bits": 100}
            with open(path, "w") as f:
                json.dump(original, f)

            app = object.__new__(twitch_bot.WebApp)
            app._settings_path = path
            app._config_lock = threading.Lock()
            # _config needs all keys _save_settings reads — use _SETTINGS_DEFAULTS
            app._config = dict(twitch_bot.WebApp._SETTINGS_DEFAULTS)
            app._config["every_n"] = 99

            # Simulate crash during json.dump
            with patch("json.dump", side_effect=OSError("disk full")):
                try:
                    app._save_settings()
                except Exception:
                    pass

            # Original file must still be readable and intact
            with open(path) as f:
                saved = json.load(f)
            self.assertEqual(saved["every_n"], 5,
                "Original settings.json must be preserved when write fails")

    def test_successful_save_updates_file(self):
        """A successful _save_settings must update the file."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w") as f:
                json.dump({"every_n": 5}, f)

            app = object.__new__(twitch_bot.WebApp)
            app._settings_path = path
            app._config_lock = threading.Lock()
            app._config = dict(twitch_bot.WebApp._SETTINGS_DEFAULTS)
            app._config["every_n"] = 99
            app._save_settings()

            with open(path) as f:
                saved = json.load(f)
            self.assertEqual(saved["every_n"], 99)


class TestAtomicFileCounter(unittest.TestCase):
    def test_crash_during_counter_write_leaves_old_value(self):
        """If os.replace fails, the counter file must retain the old value."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "hits.txt")
            with open(path, "w") as f:
                f.write("42")

            def failing_replace(src, dst):
                raise OSError("disk full")

            with patch("os.replace", side_effect=failing_replace):
                result = twitch_bot._file_counter(path)

            # Old file must be intact
            with open(path) as f:
                self.assertEqual(f.read().strip(), "42",
                    "Counter file must be intact when write fails")

    def test_counter_increments_correctly(self):
        """Normal counter increment must work."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "hits.txt")
            with open(path, "w") as f:
                f.write("10")
            result = twitch_bot._file_counter(path)
            self.assertEqual(result, "11")
            with open(path) as f:
                self.assertEqual(f.read().strip(), "11")


if __name__ == "__main__":
    unittest.main()
