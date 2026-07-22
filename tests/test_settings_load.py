# tests/test_settings_load.py
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


class TestSettingsTypeCoercion(unittest.TestCase):
    def _make_app_with_settings(self, settings_dict):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w") as f:
                json.dump(settings_dict, f)

            app = object.__new__(twitch_bot.WebApp)
            app._settings_path = path
            return app._load_settings()

    def test_min_bits_string_coerced_to_int(self):
        s = self._make_app_with_settings({"min_bits": "100"})
        self.assertIsInstance(s["min_bits"], int)
        self.assertEqual(s["min_bits"], 100)

    def test_every_n_string_coerced_to_int(self):
        s = self._make_app_with_settings({"every_n": "7"})
        self.assertIsInstance(s["every_n"], int)
        self.assertEqual(s["every_n"], 7)

    def test_thanks_cooldown_secs_string_coerced_to_int(self):
        s = self._make_app_with_settings({"thanks_cooldown_secs": "60"})
        self.assertIsInstance(s["thanks_cooldown_secs"], int)
        self.assertEqual(s["thanks_cooldown_secs"], 60)

    def test_ai_context_size_string_coerced_to_int(self):
        s = self._make_app_with_settings({"ai_context_size": "10"})
        self.assertIsInstance(s["ai_context_size"], int)
        self.assertEqual(s["ai_context_size"], 10)

    def test_invalid_int_falls_back_to_default(self):
        s = self._make_app_with_settings({"min_bits": "notanumber"})
        self.assertIsInstance(s["min_bits"], int)
        # Should be the default value from _SETTINGS_DEFAULTS
        self.assertEqual(s["min_bits"], twitch_bot.WebApp._SETTINGS_DEFAULTS["min_bits"])

    def test_int_values_unchanged(self):
        """Already-int values must not be affected."""
        s = self._make_app_with_settings({"min_bits": 50})
        self.assertEqual(s["min_bits"], 50)

    def test_missing_settings_file_uses_defaults(self):
        app = object.__new__(twitch_bot.WebApp)
        app._settings_path = "/nonexistent/path/settings.json"
        s = app._load_settings()
        self.assertIsInstance(s.get("min_bits", 0), int)
        self.assertEqual(s["min_bits"], twitch_bot.WebApp._SETTINGS_DEFAULTS["min_bits"])

    def test_all_int_fields_coerced_together(self):
        """All four int fields being strings at once are all coerced."""
        s = self._make_app_with_settings({
            "min_bits": "200",
            "every_n": "3",
            "thanks_cooldown_secs": "45",
            "ai_context_size": "8",
        })
        self.assertEqual(s["min_bits"], 200)
        self.assertEqual(s["every_n"], 3)
        self.assertEqual(s["thanks_cooldown_secs"], 45)
        self.assertEqual(s["ai_context_size"], 8)
        for key in ("min_bits", "every_n", "thanks_cooldown_secs", "ai_context_size"):
            self.assertIsInstance(s[key], int, f"{key} should be int")


if __name__ == "__main__":
    unittest.main()
