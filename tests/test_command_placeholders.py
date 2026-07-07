"""Unit tests for _apply_placeholders command response substitution."""
import unittest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from twitch_bot import _apply_placeholders


def _ph(response, username="viewer", channel="mychannel", command="!cmd", args="",
         cmd_count=0, stream_info=None, data_dir=""):
    """Convenience wrapper with defaults for new params."""
    return _apply_placeholders(response, username, channel, command, args,
                                cmd_count, stream_info, data_dir)


class TestLocalPlaceholders(unittest.TestCase):

    # ── existing ──────────────────────────────────────────────────────────────

    def test_user(self):
        self.assertEqual(_ph("Hello %user%!", "streamer"), "Hello streamer!")

    def test_channel(self):
        self.assertEqual(_ph("Welcome to %channel%!"), "Welcome to mychannel!")

    def test_command(self):
        self.assertEqual(_ph("You used %command%", command="!so"), "You used !so")

    def test_args(self):
        self.assertEqual(_ph("Go check out %args%!", args="@StreamerName"),
                         "Go check out @StreamerName!")

    def test_multiple(self):
        result = _ph("%user% used %command% with args: %args% in %channel%",
                     username="viewer", channel="mychannel", command="!so", args="@friend")
        self.assertEqual(result, "viewer used !so with args: @friend in mychannel")

    def test_unknown_left_as_is(self):
        self.assertEqual(_ph("Hello %unknown%!"), "Hello %unknown%!")

    def test_no_placeholders(self):
        self.assertEqual(_ph("Static response."), "Static response.")

    def test_empty_args(self):
        self.assertEqual(_ph("Args: '%args%'", args=""), "Args: ''")

    def test_repeated(self):
        self.assertEqual(_ph("%user% %user%", username="alice"), "alice alice")

    # ── new local ─────────────────────────────────────────────────────────────

    def test_touser_strips_at(self):
        self.assertEqual(_ph("%touser%", args="@StreamerName"), "StreamerName")

    def test_touser_no_at(self):
        self.assertEqual(_ph("%touser%", args="StreamerName"), "StreamerName")

    def test_touser_takes_first_word(self):
        self.assertEqual(_ph("%touser%", args="@Alice extra text"), "Alice")

    def test_touser_empty_args(self):
        self.assertEqual(_ph("%touser%", args=""), "")

    def test_touser_whitespace_only_args(self):
        self.assertEqual(_ph("%touser%", args="   "), "")

    def test_time_format(self):
        result = _ph("%time%")
        self.assertRegex(result, r"^\d{2}:\d{2}$")

    def test_date_format(self):
        result = _ph("%date%")
        # e.g. "July 6, 2026"
        self.assertRegex(result, r"^[A-Z][a-z]+ \d+, \d{4}$")

    def test_count(self):
        self.assertEqual(_ph("%count%", cmd_count=7), "7")

    def test_count_zero(self):
        self.assertEqual(_ph("%count%", cmd_count=0), "0")

    def test_random_default_range(self):
        for _ in range(20):
            v = int(_ph("%random%"))
            self.assertGreaterEqual(v, 1)
            self.assertLessEqual(v, 100)

    def test_random_custom_range(self):
        for _ in range(20):
            v = int(_ph("%random:5-10%"))
            self.assertGreaterEqual(v, 5)
            self.assertLessEqual(v, 10)

    def test_random_single_value_range(self):
        self.assertEqual(_ph("%random:42-42%"), "42")

    def test_random_invalid_range_left_as_is(self):
        result = _ph("%random:100-1%")
        self.assertEqual(result, "%random:100-1%")


class TestAPIPlaceholders(unittest.TestCase):

    def test_game_from_stream_info(self):
        self.assertEqual(_ph("%game%", stream_info={"game_name": "Minecraft"}), "Minecraft")

    def test_game_offline_when_empty(self):
        self.assertEqual(_ph("%game%", stream_info={}), "offline")

    def test_game_offline_when_none(self):
        self.assertEqual(_ph("%game%", stream_info=None), "offline")

    def test_title(self):
        self.assertEqual(_ph("%title%", stream_info={"title": "Playing games!"}), "Playing games!")

    def test_viewers(self):
        self.assertEqual(_ph("%viewers%", stream_info={"viewer_count": 42}), "42")

    def test_uptime_hours_and_minutes(self):
        from datetime import timezone, timedelta
        from datetime import datetime as dt
        started = (dt.now(timezone.utc) - timedelta(hours=2, minutes=14)).isoformat()
        result = _ph("%uptime%", stream_info={"started_at": started})
        self.assertIn("2h", result)
        self.assertIn("14m", result)

    def test_uptime_minutes_only(self):
        from datetime import timezone, timedelta
        from datetime import datetime as dt
        started = (dt.now(timezone.utc) - timedelta(minutes=37)).isoformat()
        result = _ph("%uptime%", stream_info={"started_at": started})
        self.assertNotIn("0h", result)
        self.assertIn("37m", result)

    def test_uptime_offline_when_no_started_at(self):
        self.assertEqual(_ph("%uptime%", stream_info={}), "offline")


class TestFilePlaceholders(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ph(self, response, **kwargs):
        return _apply_placeholders(response, "viewer", "ch", "!cmd", "",
                                    data_dir=self.tmp, **kwargs)

    # ── _safe_data_path ───────────────────────────────────────────────────────

    def test_safe_path_valid(self):
        from twitch_bot import _safe_data_path
        result = _safe_data_path(self.tmp, "facts.txt")
        self.assertEqual(result, os.path.join(self.tmp, "facts.txt"))

    def test_safe_path_rejects_traversal(self):
        from twitch_bot import _safe_data_path
        self.assertIsNone(_safe_data_path(self.tmp, "../secret.txt"))

    def test_safe_path_rejects_slash(self):
        from twitch_bot import _safe_data_path
        self.assertIsNone(_safe_data_path(self.tmp, "sub/file.txt"))

    def test_safe_path_rejects_empty_data_dir(self):
        from twitch_bot import _safe_data_path
        self.assertIsNone(_safe_data_path("", "file.txt"))

    def test_safe_path_rejects_bad_chars(self):
        from twitch_bot import _safe_data_path
        self.assertIsNone(_safe_data_path(self.tmp, "file name.txt"))

    # ── %counter% ─────────────────────────────────────────────────────────────

    def test_counter_starts_at_1_when_missing(self):
        result = self._ph("%counter:deaths.txt%")
        self.assertEqual(result, "1")
        with open(os.path.join(self.tmp, "deaths.txt")) as f:
            self.assertEqual(f.read(), "1")

    def test_counter_increments(self):
        path = os.path.join(self.tmp, "deaths.txt")
        with open(path, "w") as f:
            f.write("5")
        result = self._ph("%counter:deaths.txt%")
        self.assertEqual(result, "6")

    def test_counter_invalid_file_returns_error_string(self):
        path = os.path.join(self.tmp, "bad.txt")
        with open(path, "w") as f:
            f.write("not a number")
        result = self._ph("%counter:bad.txt%")
        self.assertEqual(result, "(invalid counter)")

    def test_counter_traversal_left_as_is(self):
        result = self._ph("%counter:../secret.txt%")
        self.assertEqual(result, "%counter:../secret.txt%")

    # ── %randomline% ──────────────────────────────────────────────────────────

    def test_randomline_picks_from_file(self):
        path = os.path.join(self.tmp, "facts.txt")
        with open(path, "w") as f:
            f.write("Fact one\nFact two\nFact three\n")
        for _ in range(10):
            result = self._ph("%randomline:facts.txt%")
            self.assertIn(result, ["Fact one", "Fact two", "Fact three"])

    def test_randomline_skips_blank_lines(self):
        path = os.path.join(self.tmp, "facts.txt")
        with open(path, "w") as f:
            f.write("Only line\n\n\n")
        self.assertEqual(self._ph("%randomline:facts.txt%"), "Only line")

    def test_randomline_all_blank_returns_empty_file(self):
        path = os.path.join(self.tmp, "blank.txt")
        with open(path, "w") as f:
            f.write("\n\n\n")
        self.assertEqual(self._ph("%randomline:blank.txt%"), "(empty file)")

    def test_randomline_file_not_found(self):
        result = self._ph("%randomline:missing.txt%")
        self.assertEqual(result, "(file not found)")

    def test_randomline_traversal_left_as_is(self):
        result = self._ph("%randomline:../etc/passwd%")
        self.assertEqual(result, "%randomline:../etc/passwd%")

    # ── %line:N% ──────────────────────────────────────────────────────────────

    def test_line_reads_correct_line(self):
        path = os.path.join(self.tmp, "quotes.txt")
        with open(path, "w") as f:
            f.write("Line one\nLine two\nLine three\n")
        self.assertEqual(self._ph("%line:2:quotes.txt%"), "Line two")

    def test_line_first_line(self):
        path = os.path.join(self.tmp, "quotes.txt")
        with open(path, "w") as f:
            f.write("Alpha\nBeta\n")
        self.assertEqual(self._ph("%line:1:quotes.txt%"), "Alpha")

    def test_line_beyond_file(self):
        path = os.path.join(self.tmp, "quotes.txt")
        with open(path, "w") as f:
            f.write("Only\n")
        self.assertEqual(self._ph("%line:99:quotes.txt%"), "(line not found)")

    def test_line_zero_invalid(self):
        path = os.path.join(self.tmp, "quotes.txt")
        with open(path, "w") as f:
            f.write("Only\n")
        self.assertEqual(self._ph("%line:0:quotes.txt%"), "(invalid line)")

    def test_line_file_not_found(self):
        self.assertEqual(self._ph("%line:1:missing.txt%"), "(file not found)")

    # ── end-to-end integration through _apply_placeholders ───────────────────

    def test_counter_end_to_end(self):
        """_replace dispatch path for %counter% works through _apply_placeholders."""
        r1 = self._ph("Deaths: %counter:e2e.txt%")
        r2 = self._ph("Deaths: %counter:e2e.txt%")
        self.assertEqual(r1, "Deaths: 1")
        self.assertEqual(r2, "Deaths: 2")

    def test_randomline_end_to_end(self):
        path = os.path.join(self.tmp, "lines.txt")
        with open(path, "w") as f:
            f.write("A\nB\nC\n")
        result = self._ph("Fact: %randomline:lines.txt%")
        self.assertIn(result, ["Fact: A", "Fact: B", "Fact: C"])

    def test_line_end_to_end(self):
        path = os.path.join(self.tmp, "q.txt")
        with open(path, "w") as f:
            f.write("Hello\nWorld\n")
        self.assertEqual(self._ph("Quote: %line:2:q.txt%"), "Quote: World")


if __name__ == "__main__":
    unittest.main()
