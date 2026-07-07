# Expanded Command Placeholders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add local, file-based, and Twitch API placeholder categories to `!command` responses, plus a Files tab in the Settings UI for managing `data/` files.

**Architecture:** `_apply_placeholders` is refactored from a static regex+dict to a callback-based `re.sub` that dispatches per placeholder type. File helpers and an API cache method live at module level and on `WebApp` respectively. Four new Flask routes serve the `data/` file manager, and a new Settings tab exposes them.

**Tech Stack:** Python 3 stdlib (`re`, `random`, `os`, `datetime`), `requests` (already imported), Flask (already imported), vanilla JS + existing CSS in `templates/index.html`.

---

### Task 1: Refactor `_apply_placeholders` with new signature + local placeholders

**Files:**
- Modify: `twitch_bot.py` lines 105–114 (`_PLACEHOLDER_RE` and `_apply_placeholders`)
- Modify: `twitch_bot.py` line 31–46 (add `import random`)
- Modify: `tests/test_command_placeholders.py`

- [ ] **Step 1: Add `import random` to twitch_bot.py**

In `twitch_bot.py`, the import block starts at line 31. Insert `import random` after `import re` (line 36):

```python
import random
import re
```

- [ ] **Step 2: Write failing tests for new local placeholders**

Replace the entire contents of `tests/test_command_placeholders.py`:

```python
"""Unit tests for _apply_placeholders command response substitution."""
import unittest
import sys
import os
from unittest.mock import patch

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

    def test_time_format(self):
        import re
        result = _ph("%time%")
        self.assertRegex(result, r"^\d{2}:\d{2}$")

    def test_date_format(self):
        import re
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests — expect failures**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
python -m pytest tests/test_command_placeholders.py -v 2>&1 | tail -30
```

Expected: most new tests FAIL (TypeError or wrong values).

- [ ] **Step 4: Replace `_apply_placeholders` in twitch_bot.py**

Replace lines 105–114 (the `_PLACEHOLDER_RE` constant and the full `_apply_placeholders` function):

```python
_PLACEHOLDER_RE = re.compile(r"%[a-zA-Z0-9_]+(?::[^%]+)?%")


def _calc_uptime(started_at: str) -> str:
    from datetime import timezone
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - start
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m = rem // 60
        return f"{h}h {m}m" if h else f"{m}m"
    except Exception:
        return "offline"


def _apply_placeholders(
    response: str,
    username: str,
    channel: str,
    command: str,
    args: str,
    cmd_count: int = 0,
    stream_info: dict | None = None,
    data_dir: str = "",
) -> str:
    if stream_info is None:
        stream_info = {}
    now = datetime.now()
    touser = args.split()[0].lstrip("@") if args.split() else ""
    static: dict[str, str] = {
        "%user%":    username,
        "%channel%": channel,
        "%command%": command,
        "%args%":    args,
        "%touser%":  touser,
        "%time%":    now.strftime("%H:%M"),
        "%date%":    now.strftime("%B ") + str(now.day) + now.strftime(", %Y"),
        "%count%":   str(cmd_count),
        "%game%":    stream_info.get("game_name", "offline"),
        "%title%":   stream_info.get("title", "offline"),
        "%viewers%": str(stream_info["viewer_count"]) if "viewer_count" in stream_info else "offline",
        "%uptime%":  _calc_uptime(stream_info["started_at"]) if "started_at" in stream_info else "offline",
        "%random%":  str(random.randint(1, 100)),
    }

    def _replace(m: re.Match) -> str:
        token = m.group(0)
        if token in static:
            return static[token]
        rm = re.fullmatch(r"%random:(\d+)-(\d+)%", token)
        if rm:
            lo, hi = int(rm.group(1)), int(rm.group(2))
            return str(random.randint(lo, hi)) if lo <= hi else token
        cm = re.fullmatch(r"%counter:([^%]+)%", token)
        if cm:
            path = _safe_data_path(data_dir, cm.group(1))
            return _file_counter(path) if path else token
        rl = re.fullmatch(r"%randomline:([^%]+)%", token)
        if rl:
            path = _safe_data_path(data_dir, rl.group(1))
            return _file_random_line(path) if path else token
        lm = re.fullmatch(r"%line:(\d+):([^%]+)%", token)
        if lm:
            path = _safe_data_path(data_dir, lm.group(2))
            return _file_line(path, int(lm.group(1))) if path else token
        return token

    return _PLACEHOLDER_RE.sub(_replace, response)
```

Note: `_safe_data_path`, `_file_counter`, `_file_random_line`, and `_file_line` are referenced here but defined in Task 2. Tests that exercise them will fail until Task 2 is complete — the local+API tests should pass now.

- [ ] **Step 5: Run local+API tests — expect them to pass (file tests will error)**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
python -m pytest tests/test_command_placeholders.py::TestLocalPlaceholders tests/test_command_placeholders.py::TestAPIPlaceholders -v 2>&1 | tail -30
```

Expected: all `TestLocalPlaceholders` and `TestAPIPlaceholders` pass. The full suite errors on `_safe_data_path` name not yet defined — that's fine.

- [ ] **Step 6: Commit**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
git add twitch_bot.py tests/test_command_placeholders.py
git commit -m "feat(placeholders): refactor _apply_placeholders with local + API placeholder support"
```

---

### Task 2: File helper functions + file-based placeholder dispatch

**Files:**
- Modify: `twitch_bot.py` — add `_safe_data_path`, `_file_counter`, `_file_random_line`, `_file_line` after `_calc_uptime` (new module-level functions)

- [ ] **Step 1: Write failing tests for file-based placeholders**

Add this class to `tests/test_command_placeholders.py` (append before `if __name__ == "__main__":`):

```python
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
```

- [ ] **Step 2: Run tests — expect failures**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
python -m pytest tests/test_command_placeholders.py::TestFilePlaceholders -v 2>&1 | tail -30
```

Expected: NameError on `_safe_data_path`.

- [ ] **Step 3: Add file helper functions to twitch_bot.py**

Insert these four functions immediately after `_calc_uptime` (after the closing of that function, before `_apply_placeholders`):

```python
_DATA_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


def _safe_data_path(data_dir: str, name: str) -> str | None:
    if not data_dir:
        return None
    if not _DATA_NAME_RE.fullmatch(name):
        return None
    if ".." in name:
        return None
    return os.path.join(data_dir, name)


def _file_counter(path: str) -> str:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as f:
                val = int(f.read().strip())
        except FileNotFoundError:
            val = 0
        except ValueError:
            return "(invalid counter)"
        val += 1
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(val))
        return str(val)
    except Exception:
        return "(error)"


def _file_random_line(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln.rstrip() for ln in f if ln.strip()]
        return random.choice(lines) if lines else "(file not found)"
    except FileNotFoundError:
        return "(file not found)"
    except Exception:
        return "(error)"


def _file_line(path: str, n: int) -> str:
    if n < 1:
        return "(invalid line)"
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if n > len(lines):
            return "(line not found)"
        return lines[n - 1].rstrip()
    except FileNotFoundError:
        return "(file not found)"
    except Exception:
        return "(error)"
```

- [ ] **Step 4: Run all placeholder tests**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
python -m pytest tests/test_command_placeholders.py -v 2>&1 | tail -40
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
git add twitch_bot.py tests/test_command_placeholders.py
git commit -m "feat(placeholders): add file-based placeholders (counter, randomline, line:N)"
```

---

### Task 3: WebApp state + `_fetch_stream_info()` + update call site

**Files:**
- Modify: `twitch_bot.py` — `WebApp.__init__` (~line 1230) and new `_fetch_stream_info` method, and `_route_chat_commands` (~line 2168)

- [ ] **Step 1: Add new instance variables to `WebApp.__init__`**

Find these two existing lines (~line 1230–1231):

```python
        self._cmd_global_cooldowns: dict[str, float]             = {}
        self._cmd_user_cooldowns:   dict[tuple[str, str], float] = {}
```

Add three more lines immediately after them:

```python
        self._cmd_use_counts:   dict[str, int]  = {}
        self._data_dir          = os.path.join(_here, "data")
        self._stream_cache:     dict             = {}
        self._stream_cache_ts:  float            = 0.0
```

- [ ] **Step 2: Add `_fetch_stream_info` method to WebApp**

Find the `_route_chat_commands` method (~line 2136). Insert this method immediately before it:

```python
    def _fetch_stream_info(self) -> dict:
        with self._config_lock:
            if time.time() - self._stream_cache_ts < 60:
                return dict(self._stream_cache)
            channel   = self._config.get("twitch_channel", "")
            client_id = self._config.get("twitch_client_id", "")
            token     = (self._config.get("twitch_token", "")
                         or self._config.get("bot_token", ""))
        if not channel or not client_id or not token:
            return {}
        try:
            resp = requests.get(
                "https://api.twitch.tv/helix/streams",
                params={"user_login": channel},
                headers={
                    "Client-ID": client_id,
                    "Authorization": f"Bearer {token.removeprefix('oauth:')}",
                },
                timeout=5,
            )
            data   = resp.json().get("data", [])
            result = data[0] if data else {}
        except Exception as exc:
            self._log(f"[StreamInfo] Fetch failed: {exc}")
            result = {}
        with self._config_lock:
            self._stream_cache    = result
            self._stream_cache_ts = time.time()
        return dict(result)

```

- [ ] **Step 3: Update `_route_chat_commands` call site**

Find these lines in `_route_chat_commands` (~line 2167–2172):

```python
            response = entry.get("response", "")
            args     = message.strip()[len(word):].strip()
            response = _apply_placeholders(response, username, channel, word, args)
            irc = self._irc
```

Replace with:

```python
            response = entry.get("response", "")
            args     = message.strip()[len(word):].strip()
            self._cmd_use_counts[word] = self._cmd_use_counts.get(word, 0) + 1
            count       = self._cmd_use_counts[word]
            stream_info = self._fetch_stream_info()
            response    = _apply_placeholders(
                response, username, channel, word, args,
                count, stream_info, self._data_dir,
            )
            irc = self._irc
```

- [ ] **Step 4: Smoke-test the bot starts without errors**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
timeout 5 .venv/bin/python twitch_bot.py 2>&1 | head -20
```

Expected: `[System] Ready.` line appears, no tracebacks. (It may show connection errors — that's fine, credentials aren't set in the test environment.)

- [ ] **Step 5: Run full test suite**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all existing tests still pass.

- [ ] **Step 6: Commit**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
git add twitch_bot.py
git commit -m "feat(placeholders): add stream API cache and wire new args into _route_chat_commands"
```

---

### Task 4: Flask `/api/datafiles` routes

**Files:**
- Modify: `twitch_bot.py` — `_register_routes` method, after the `# ── voices` block (~line 1930)

- [ ] **Step 1: Write failing Flask tests**

Create `tests/test_datafiles_api.py`:

```python
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
        # Patch data_dir before importing WebApp to point at tmp
        import twitch_bot
        from unittest.mock import patch, MagicMock
        # Start the app with minimal mocking so Flask routes register
        with patch.object(twitch_bot, '_load_env', return_value={}), \
             patch.object(twitch_bot, '_load_settings', return_value={}), \
             patch('twitch_bot.TTSEngine'), \
             patch('twitch_bot.AIResponseHandler'), \
             patch('twitch_bot.threading.Timer'), \
             patch('twitch_bot.threading.Thread'):
            self.app_obj = twitch_bot.WebApp.__new__(twitch_bot.WebApp)
            self.app_obj._here         = self.tmp
            self.app_obj._data_dir     = os.path.join(self.tmp, "data")
            self.app_obj._prompts_dir  = os.path.join(self.tmp, "prompts")
            self.app_obj._presets_dir  = os.path.join(self.tmp, "presets")
            self.app_obj._config_lock  = __import__('threading').Lock()
            self.app_obj._config       = {}
            self.app_obj._stream_cache = {}
            self.app_obj._stream_cache_ts = 0.0
            import flask as _flask
            self.app_obj._flask = _flask.Flask(__name__)
            self.app_obj._register_routes()
        self.client = self.app_obj._flask.test_client()

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
        resp = self.client.get("/api/datafiles/../etc/passwd")
        self.assertIn(resp.status_code, [400, 404])

    # ── POST /api/datafiles/<name> ────────────────────────────────────────────

    def test_create_new_file(self):
        resp = self.client.post("/api/datafiles/new.txt",
                                data=json.dumps({"content": "line1\nline2"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        path = os.path.join(self.tmp, "data", "new.txt")
        with open(path) as f:
            self.assertEqual(f.read(), "line1\nline2")

    def test_overwrite_existing_file(self):
        self._make_file("deaths.txt", "5")
        self.client.post("/api/datafiles/deaths.txt",
                         data=json.dumps({"content": "10"}),
                         content_type="application/json")
        path = os.path.join(self.tmp, "data", "deaths.txt")
        with open(path) as f:
            self.assertEqual(f.read(), "10")

    def test_create_invalid_name(self):
        resp = self.client.post("/api/datafiles/../secret",
                                data=json.dumps({"content": "x"}),
                                content_type="application/json")
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
        resp = self.client.delete("/api/datafiles/../etc/passwd")
        self.assertIn(resp.status_code, [400, 404])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run — expect failures**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
python -m pytest tests/test_datafiles_api.py -v 2>&1 | tail -20
```

Expected: 404 on all routes (routes don't exist yet).

- [ ] **Step 3: Add `/api/datafiles` routes to `_register_routes`**

Find the `# ── voices` block (~line 1930). After the closing of `api_voices` and before `# ── file browser`, add:

```python
        # ── data files ───────────────────────────────────────────────────────

        @app.route("/api/datafiles")
        def api_datafiles_list():
            os.makedirs(self._data_dir, exist_ok=True)
            try:
                files = sorted(
                    [{"name": e.name, "size": e.stat().st_size}
                     for e in os.scandir(self._data_dir)
                     if e.is_file() and _DATA_NAME_RE.fullmatch(e.name)],
                    key=lambda x: x["name"],
                )
            except Exception:
                files = []
            return _flask.jsonify({"files": files})

        @app.route("/api/datafiles/<name>", methods=["GET"])
        def api_datafile_read(name: str):
            path = _safe_data_path(self._data_dir, name)
            if path is None:
                return _flask.jsonify({"error": "invalid name"}), 400
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()
            except FileNotFoundError:
                return _flask.jsonify({"error": "not found"}), 404
            except Exception:
                return _flask.jsonify({"error": "read error"}), 500
            return _flask.jsonify({"name": name, "content": content})

        @app.route("/api/datafiles/<name>", methods=["POST"])
        def api_datafile_save(name: str):
            path = _safe_data_path(self._data_dir, name)
            if path is None:
                return _flask.jsonify({"error": "invalid name"}), 400
            data    = _flask.request.get_json(force=True, silent=True) or {}
            content = data.get("content", "")
            os.makedirs(self._data_dir, exist_ok=True)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as exc:
                return _flask.jsonify({"error": str(exc)}), 500
            return _flask.jsonify({"ok": True, "name": name})

        @app.route("/api/datafiles/<name>", methods=["DELETE"])
        def api_datafile_delete(name: str):
            path = _safe_data_path(self._data_dir, name)
            if path is None:
                return _flask.jsonify({"error": "invalid name"}), 400
            try:
                os.remove(path)
            except FileNotFoundError:
                return _flask.jsonify({"error": "not found"}), 404
            except Exception as exc:
                return _flask.jsonify({"error": str(exc)}), 500
            return _flask.jsonify({"ok": True})

```

- [ ] **Step 4: Run API tests**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
python -m pytest tests/test_datafiles_api.py -v 2>&1 | tail -25
```

Expected: all tests pass.

- [ ] **Step 5: Run full test suite**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
git add twitch_bot.py tests/test_datafiles_api.py
git commit -m "feat(placeholders): add /api/datafiles CRUD routes for data/ file manager"
```

---

### Task 5: Files tab in Settings UI

**Files:**
- Modify: `templates/index.html`

No automated tests — verify manually after committing by opening the bot UI.

- [ ] **Step 1: Add the Files tab button**

Find the tab buttons in `templates/index.html` (line ~252–253):

```html
      <button class="tab"        onclick="showTab('commands')">Commands</button>
      <button class="tab"        onclick="showTab('schedule')">Schedule</button>
```

Replace with:

```html
      <button class="tab"        onclick="showTab('commands')">Commands</button>
      <button class="tab"        onclick="showTab('files')">Files</button>
      <button class="tab"        onclick="showTab('schedule')">Schedule</button>
```

- [ ] **Step 2: Add the Files tab pane**

Find the closing of the Commands tab pane and the start of the Schedule tab pane (~line 464–467):

```html
      </div>

      <!-- Schedule tab -->
```

Insert the new Files tab pane between them:

```html
      </div>

      <!-- Files tab -->
      <div id="tab-files" class="tab-pane">
        <div class="section-lbl">Data Files</div>
        <div class="hint">Files in the <code>data/</code> folder. Reference them in !command responses with <code>%randomline:file.txt%</code>, <code>%counter:file.txt%</code>, or <code>%line:N:file.txt%</code>.</div>
        <div class="divider"></div>
        <table id="datafiles-table" style="width:100%;border-collapse:collapse;margin-bottom:8px">
          <thead>
            <tr>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">File</th>
              <th style="text-align:right;padding:4px 6px;font-size:11px;color:var(--muted)">Size</th>
              <th style="width:90px"></th>
            </tr>
          </thead>
          <tbody id="datafiles-rows"></tbody>
        </table>
        <button class="btn btn-neutral btn-sm" onclick="openFileEditor(null)">+ New file</button>
        <div id="datafile-editor" style="display:none;margin-top:12px">
          <div style="display:flex;gap:8px;margin-bottom:6px;align-items:center">
            <input id="datafile-name" type="text" placeholder="filename.txt" style="flex:0 0 200px">
            <span id="datafile-editor-hint" style="color:var(--muted);font-size:12px">New file</span>
          </div>
          <textarea id="datafile-content" rows="8" style="width:100%;box-sizing:border-box;resize:vertical" placeholder="File contents…"></textarea>
          <div style="display:flex;gap:8px;margin-top:6px">
            <button class="btn btn-green btn-sm" onclick="saveDataFile()">Save</button>
            <button class="btn btn-neutral btn-sm" onclick="closeFileEditor()">Cancel</button>
          </div>
        </div>
      </div>

      <!-- Schedule tab -->
```

- [ ] **Step 3: Hook `loadDataFiles` into `showTab`**

Find `showTab` in `templates/index.html` (~line 964–972):

```javascript
function showTab(name) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  el('tab-' + name).classList.add('active');
  document.querySelectorAll('.tab').forEach(b => {
    if (b.textContent.toLowerCase().startsWith(name)) b.classList.add('active');
  });
  if (name === 'tts') refreshVoices();
}
```

Replace with:

```javascript
function showTab(name) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  el('tab-' + name).classList.add('active');
  document.querySelectorAll('.tab').forEach(b => {
    if (b.textContent.toLowerCase().startsWith(name)) b.classList.add('active');
  });
  if (name === 'tts') refreshVoices();
  if (name === 'files') loadDataFiles();
}
```

- [ ] **Step 4: Add JS functions for the file manager**

Find `function escHtml` (~line 1094). Insert the following block immediately before it:

```javascript
// ── Data file manager ─────────────────────────────────────────────────────

let _editingFile = null;

function loadDataFiles() {
  api('/api/datafiles').then(d => {
    const tb = el('datafiles-rows');
    tb.innerHTML = '';
    (d.files || []).forEach(f => {
      const tr = document.createElement('tr');
      tr.innerHTML =
        `<td style="padding:4px 6px;font-size:12px">${escHtml(f.name)}</td>` +
        `<td style="padding:4px 6px;font-size:11px;color:var(--muted);text-align:right">${f.size} B</td>` +
        `<td style="padding:4px 2px;text-align:right;white-space:nowrap">` +
          `<button class="btn btn-neutral btn-sm" onclick="openFileEditor('${escHtml(f.name)}')">Edit</button> ` +
          `<button class="btn btn-red btn-sm" onclick="deleteDataFile('${escHtml(f.name)}')">✕</button>` +
        `</td>`;
      tb.appendChild(tr);
    });
  });
}

function openFileEditor(name) {
  _editingFile = name;
  const nameInput = el('datafile-name');
  const hint      = el('datafile-editor-hint');
  el('datafile-content').value = '';
  el('datafile-editor').style.display = 'block';
  if (name) {
    nameInput.value    = name;
    nameInput.readOnly = true;
    hint.textContent   = 'Editing: ' + name;
    api('/api/datafiles/' + encodeURIComponent(name)).then(d => {
      el('datafile-content').value = d.content || '';
    });
  } else {
    nameInput.value    = '';
    nameInput.readOnly = false;
    hint.textContent   = 'New file';
  }
}

function closeFileEditor() {
  el('datafile-editor').style.display = 'none';
  _editingFile = null;
}

function saveDataFile() {
  const name    = el('datafile-name').value.trim();
  const content = el('datafile-content').value;
  if (!name) return;
  api('/api/datafiles/' + encodeURIComponent(name), 'POST', { content })
    .then(() => { closeFileEditor(); loadDataFiles(); });
}

function deleteDataFile(name) {
  if (!confirm('Delete ' + name + '?')) return;
  fetch('/api/datafiles/' + encodeURIComponent(name), { method: 'DELETE' })
    .then(() => loadDataFiles());
}

```

- [ ] **Step 5: Verify the UI manually**

Start the bot:
```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
.venv/bin/python twitch_bot.py
```

Open `http://localhost:5000` in a browser, click the gear icon, click the **Files** tab. Verify:
- The tab appears between Commands and Schedule
- The file list loads (empty initially)
- Clicking "+ New file" shows the editor with a filename input
- Saving creates the file (check `data/` folder)
- Clicking Edit loads content; saving overwrites
- Clicking ✕ with confirmation deletes the file
- List refreshes after each action

- [ ] **Step 6: Commit**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
git add templates/index.html
git commit -m "feat(placeholders): add Files tab in Settings UI for data/ file manager"
```

---

### Task 6: Update `Placeholder.md`

**Files:**
- Modify: `Placeholder.md`

- [ ] **Step 1: Rewrite `Placeholder.md` with full placeholder set**

Replace the entire file:

```markdown
# Command Response Placeholders

Use these placeholders in your custom `!command` response text. They are replaced at runtime when the command fires in chat.

---

## Local Placeholders

No API or files needed — computed instantly at call time.

| Placeholder | Value | Example output |
|---|---|---|
| `%user%` | Twitch username of the person who typed the command | `streamer42` |
| `%channel%` | Twitch channel name | `mychannel` |
| `%command%` | The command word itself | `!so` |
| `%args%` | Everything typed after the command word (empty if nothing) | `@StreamerName` |
| `%touser%` | First word of `%args%` with `@` stripped — useful for shoutouts | `StreamerName` |
| `%time%` | Current server time (24-hour) | `14:32` |
| `%date%` | Current server date | `July 6, 2026` |
| `%count%` | How many times this command has fired this session (resets on bot restart) | `7` |
| `%random%` | Random integer from 1 to 100 | `42` |
| `%random:MIN-MAX%` | Random integer in your range | `%random:1-1000%` → `537` |

---

## File-Based Placeholders

Files live in the `data/` folder next to `twitch_bot.py`. Manage them via **Settings → Files**.

Filenames must use only letters, numbers, underscores, hyphens, and dots (`a-z A-Z 0-9 _ - .`).

| Placeholder | Value |
|---|---|
| `%counter:filename%` | Reads a number from the file, adds 1, saves it back, returns the new value. Creates the file at 0 if missing. |
| `%randomline:filename%` | Picks a random non-empty line from the file. Returns `(file not found)` if the file is missing or empty. |
| `%line:N:filename%` | Returns line N (1-indexed) from the file. Returns `(line not found)` if N is out of range. |

### Examples

**Death counter** — increments every time someone runs `!deaths`:

Command: `!deaths`
Response: `The streamer has died %counter:deaths.txt% times today.`

---

**Fun fact** — picks a random line from `data/facts.txt`:

Command: `!fact`
Response: `Fun fact: %randomline:facts.txt%`

---

**Quote of the day** — always returns line 1 of `data/quotes.txt`:

Command: `!quote`
Response: `"%line:1:quotes.txt%"`

---

## API Placeholders

Pulls live data from Twitch. Requires your Broadcaster Token and Client ID to be configured. Cached for 60 seconds. Returns `offline` if the stream is not live or credentials are missing.

| Placeholder | Value |
|---|---|
| `%game%` | Current game or category |
| `%title%` | Stream title |
| `%uptime%` | How long the stream has been live — `2h 14m` |
| `%viewers%` | Current viewer count |

---

## Notes

- Placeholders are case-sensitive — use lowercase exactly as shown.
- Unknown or misspelled placeholders (e.g. `%usr%`) are left as-is in the output.
- `%args%` is an empty string if the user types the command with no arguments.
- `%touser%` is an empty string if there are no args.
- The full response (after substitution) is capped at 500 characters before posting.
- `%command%` always expands to the lowercase form of the command (e.g. `!so` even if typed as `!SO`).
- `%count%` tracks uses in the current bot session only. Use `%counter:file%` for an all-time persistent count.
```

- [ ] **Step 2: Commit**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
git add Placeholder.md
git commit -m "docs: update Placeholder.md with full expanded placeholder reference"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| `%touser%`, `%time%`, `%date%`, `%count%`, `%random%`, `%random:MIN-MAX%` | Task 1 |
| `%counter:f%`, `%randomline:f%`, `%line:N:f%` | Task 2 |
| `_apply_placeholders` new signature | Task 1 |
| `_safe_data_path` path sanitisation | Task 2 |
| `_cmd_use_counts` session counter | Task 3 |
| `_fetch_stream_info()` with 60 s cache | Task 3 |
| `%game%`, `%title%`, `%uptime%`, `%viewers%` | Task 1 + Task 3 |
| `/api/datafiles` CRUD routes | Task 4 |
| Files tab UI (list, edit, create, delete) | Task 5 |
| `Placeholder.md` updated | Task 6 |
| `data_dir` created on first use | Task 4 (routes call `os.makedirs`) |
| File I/O errors return `(error)`, log | Task 2 |
| Unknown placeholders left as-is | Task 1 |

All spec requirements covered. No gaps found.
