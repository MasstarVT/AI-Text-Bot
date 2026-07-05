# Command Cooldowns and Auto-!commands List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-command cooldowns (global or per-user, silent ignore on cooldown) and an optional auto-generated `!commands` list to the existing custom commands system.

**Architecture:** Extend `chat_commands` from `dict[str, str]` to `dict[str, dict]` (each value has `response`, `cooldown`, `cooldown_type`). Two in-memory dicts on `WebApp` track cooldown timestamps. A new `cmd_list_enabled` bool config key enables the auto-list. Existing string values are migrated to the new format on load. Frontend table gains two new columns; a new checkbox controls the auto-list toggle.

**Tech Stack:** Python 3, Flask, `twitch_bot.py` (single-file backend), `templates/index.html` (single-file frontend), `unittest` for tests.

---

## Files

| File | Change |
|---|---|
| `twitch_bot.py` | Add cooldown dicts to `__init__`, update `_SETTINGS_DEFAULTS`, config init migration, `_save_settings`, `_SETTINGS_KEYS`, `_BOOL_KEYS`, POST handler, `_route_chat_commands` rewrite |
| `templates/index.html` | Add Cooldown/Mode columns to table, `!commands` toggle checkbox, update `addCmdRow`, `getChatCommands`, `openSettings`, `saveSettings` |
| `tests/test_command_cooldowns.py` | New test file (10 tests) |
| `CLAUDE.md` | Update Custom `!command` responses section |

---

### Task 1: Tests + backend foundation

**Files:**
- Create: `tests/test_command_cooldowns.py`
- Modify: `twitch_bot.py:1129` (`_SETTINGS_DEFAULTS`), `twitch_bot.py:1209` (config init), `twitch_bot.py:1224` (`__init__` instance vars), `twitch_bot.py:1490-1491` (`_save_settings`)

- [ ] **Step 1: Write the test file**

Create `tests/test_command_cooldowns.py`:

```python
"""Tests for per-command cooldowns and auto-!commands list."""
import collections
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


def _make_app(commands: dict, cmd_list_enabled: bool = False) -> twitch_bot.WebApp:
    """Construct a minimal WebApp stub for _route_chat_commands tests."""
    app = object.__new__(twitch_bot.WebApp)
    app._config_lock = threading.Lock()
    app._config = {
        "chat_commands_enabled": True,
        "chat_commands": commands,
        "twitch_channel": "testchannel",
        "cmd_list_enabled": cmd_list_enabled,
    }
    app._irc = MagicMock()
    app._log = lambda msg: None
    app._cmd_global_cooldowns = {}
    app._cmd_user_cooldowns   = {}
    return app


_CMD_GLOBAL = {"response": "Hey %user%!", "cooldown": 30, "cooldown_type": "global"}
_CMD_USER   = {"response": "Shoutout!",   "cooldown": 10, "cooldown_type": "user"}
_CMD_FREE   = {"response": "Free!",       "cooldown": 0,  "cooldown_type": "global"}


class TestGlobalCooldown(unittest.TestCase):
    def test_blocks_repeat_within_window(self):
        """Second call within cooldown window is silently dropped."""
        app = _make_app({"!hi": _CMD_GLOBAL})
        with patch("twitch_bot.time.time", return_value=1000.0):
            app._route_chat_commands("viewer1", "!hi")
        with patch("twitch_bot.time.time", return_value=1010.0):  # 10s < 30s
            app._route_chat_commands("viewer2", "!hi")
        self.assertEqual(app._irc.say.call_count, 1)

    def test_allows_after_window(self):
        """Call after cooldown window fires normally."""
        app = _make_app({"!hi": _CMD_GLOBAL})
        with patch("twitch_bot.time.time", return_value=1000.0):
            app._route_chat_commands("viewer1", "!hi")
        with patch("twitch_bot.time.time", return_value=1031.0):  # 31s > 30s
            app._route_chat_commands("viewer2", "!hi")
        self.assertEqual(app._irc.say.call_count, 2)

    def test_blocks_regardless_of_viewer(self):
        """Global cooldown applies to all viewers, not just the one who triggered."""
        app = _make_app({"!hi": _CMD_GLOBAL})
        with patch("twitch_bot.time.time", return_value=1000.0):
            app._route_chat_commands("viewer1", "!hi")
        with patch("twitch_bot.time.time", return_value=1005.0):
            app._route_chat_commands("viewer1", "!hi")
        self.assertEqual(app._irc.say.call_count, 1)


class TestPerUserCooldown(unittest.TestCase):
    def test_blocks_same_viewer_during_window(self):
        """Same viewer cannot trigger again during their per-user cooldown."""
        app = _make_app({"!so": _CMD_USER})
        with patch("twitch_bot.time.time", return_value=1000.0):
            app._route_chat_commands("viewer1", "!so")
        with patch("twitch_bot.time.time", return_value=1005.0):  # 5s < 10s
            app._route_chat_commands("viewer1", "!so")
        self.assertEqual(app._irc.say.call_count, 1)

    def test_allows_different_viewer_during_window(self):
        """Different viewer is not affected by another viewer's cooldown."""
        app = _make_app({"!so": _CMD_USER})
        with patch("twitch_bot.time.time", return_value=1000.0):
            app._route_chat_commands("viewer1", "!so")
        with patch("twitch_bot.time.time", return_value=1005.0):
            app._route_chat_commands("viewer2", "!so")
        self.assertEqual(app._irc.say.call_count, 2)


class TestZeroCooldown(unittest.TestCase):
    def test_zero_cooldown_never_blocks(self):
        """Commands with cooldown=0 always fire regardless of frequency."""
        app = _make_app({"!free": _CMD_FREE})
        for _ in range(5):
            app._route_chat_commands("viewer1", "!free")
        self.assertEqual(app._irc.say.call_count, 5)


class TestMigration(unittest.TestCase):
    def test_string_value_promoted_to_dict(self):
        """Legacy string command values are migrated to dict format."""
        raw = {"!hi": "Hello there!"}
        migrated = {
            k: (v if isinstance(v, dict)
                else {"response": v, "cooldown": 0, "cooldown_type": "global"})
            for k, v in raw.items()
        }
        self.assertEqual(migrated["!hi"]["response"], "Hello there!")
        self.assertEqual(migrated["!hi"]["cooldown"], 0)
        self.assertEqual(migrated["!hi"]["cooldown_type"], "global")


class TestAutoCommandsList(unittest.TestCase):
    def test_auto_list_fires_when_enabled(self):
        """!commands posts a sorted, prefixed list when enabled and no user entry."""
        cmds = {
            "!so":    {"response": "Shout!", "cooldown": 0, "cooldown_type": "global"},
            "!hello": {"response": "Hey!",   "cooldown": 0, "cooldown_type": "global"},
        }
        app = _make_app(cmds, cmd_list_enabled=True)
        app._route_chat_commands("viewer", "!commands")
        app._irc.say.assert_called_once()
        _, reply = app._irc.say.call_args[0]
        self.assertTrue(reply.startswith("Commands:"))
        self.assertIn("!hello", reply)
        self.assertIn("!so", reply)

    def test_auto_list_suppressed_by_user_entry(self):
        """User-defined !commands entry takes priority over auto-list."""
        cmds = {
            "!commands": {"response": "Custom list!", "cooldown": 0, "cooldown_type": "global"},
            "!so":       {"response": "Shout!",       "cooldown": 0, "cooldown_type": "global"},
        }
        app = _make_app(cmds, cmd_list_enabled=True)
        app._route_chat_commands("viewer", "!commands")
        app._irc.say.assert_called_once_with("testchannel", "Custom list!")

    def test_auto_list_suppressed_when_disabled(self):
        """!commands does nothing when cmd_list_enabled=False and no user entry."""
        cmds = {"!so": {"response": "Shout!", "cooldown": 0, "cooldown_type": "global"}}
        app = _make_app(cmds, cmd_list_enabled=False)
        app._route_chat_commands("viewer", "!commands")
        app._irc.say.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
python -m pytest tests/test_command_cooldowns.py -v
```

Expected: most tests fail with `AttributeError` (`_cmd_global_cooldowns` not on app, `_route_chat_commands` not updated yet). `TestMigration` may pass as it tests logic inline.

- [ ] **Step 3: Add cooldown dicts to `WebApp.__init__`**

In `twitch_bot.py`, after line 1223 (`self._thanks_lock = threading.Lock()`), insert:

```python
        self._cmd_global_cooldowns: dict[str, float]             = {}
        self._cmd_user_cooldowns:   dict[tuple[str, str], float] = {}
```

- [ ] **Step 4: Add `cmd_list_enabled` to `_SETTINGS_DEFAULTS`**

In `twitch_bot.py`, find lines 1128-1129:
```python
        "chat_commands_enabled": False,
        "chat_commands":         {},
```

Replace with:
```python
        "chat_commands_enabled": False,
        "chat_commands":         {},
        "cmd_list_enabled":      False,
```

- [ ] **Step 5: Update config init to migrate `chat_commands` values**

In `twitch_bot.py`, find line 1209:
```python
            "chat_commands":         dict(settings.get("chat_commands", {})),
```

Replace with:
```python
            "chat_commands": {
                k: (v if isinstance(v, dict)
                    else {"response": v, "cooldown": 0, "cooldown_type": "global"})
                for k, v in settings.get("chat_commands", {}).items()
            },
            "cmd_list_enabled": settings.get("cmd_list_enabled", False),
```

- [ ] **Step 6: Update `_save_settings` to persist `cmd_list_enabled`**

In `twitch_bot.py`, find lines 1490-1491:
```python
            "chat_commands_enabled": c.get("chat_commands_enabled", False),
            "chat_commands":         c.get("chat_commands",         {}),
```

Replace with:
```python
            "chat_commands_enabled": c.get("chat_commands_enabled", False),
            "chat_commands":         c.get("chat_commands",         {}),
            "cmd_list_enabled":      c.get("cmd_list_enabled",      False),
```

- [ ] **Step 7: Run migration test to confirm it passes**

```bash
python -m pytest tests/test_command_cooldowns.py::TestMigration -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add tests/test_command_cooldowns.py twitch_bot.py
git commit -m "test(commands): add cooldown and auto-list tests; add cooldown dicts and defaults"
```

---

### Task 2: Settings API

**Files:**
- Modify: `twitch_bot.py:1693` (`_SETTINGS_KEYS`), `twitch_bot.py:1708-1718` (`_BOOL_KEYS`), `twitch_bot.py:1725-1736` (`chat_commands` POST parsing)

- [ ] **Step 1: Add `cmd_list_enabled` to `_SETTINGS_KEYS`**

In `twitch_bot.py`, find line 1693:
```python
            "chat_commands_enabled", "chat_commands",
```

Replace with:
```python
            "chat_commands_enabled", "chat_commands",
            "cmd_list_enabled",
```

- [ ] **Step 2: Add `cmd_list_enabled` to `_BOOL_KEYS`**

In `twitch_bot.py`, find line 1716:
```python
                "chat_commands_enabled",
```

Replace with:
```python
                "chat_commands_enabled",
                "cmd_list_enabled",
```

- [ ] **Step 3: Update `chat_commands` POST handler to parse dict values**

In `twitch_bot.py`, find lines 1725-1736:
```python
                        elif k == "chat_commands":
                            if isinstance(data[k], dict):
                                cmds = {}
                                for cmd, resp in data[k].items():
                                    cmd  = str(cmd).lower().strip()
                                    resp = str(resp).strip()
                                    if not cmd or not resp:
                                        continue
                                    if not cmd.startswith("!"):
                                        cmd = "!" + cmd
                                    cmds[cmd] = resp
                                self._config[k] = cmds
```

Replace with:
```python
                        elif k == "chat_commands":
                            if isinstance(data[k], dict):
                                cmds = {}
                                for cmd, entry in data[k].items():
                                    cmd = str(cmd).lower().strip()
                                    if not cmd.startswith("!"):
                                        cmd = "!" + cmd
                                    if isinstance(entry, dict):
                                        response = str(entry.get("response", "")).strip()
                                        if not cmd or not response:
                                            continue
                                        try:
                                            cooldown = max(0, int(entry.get("cooldown", 0)))
                                        except (TypeError, ValueError):
                                            cooldown = 0
                                        cooldown_type = str(entry.get("cooldown_type", "global"))
                                        if cooldown_type not in ("global", "user"):
                                            cooldown_type = "global"
                                        cmds[cmd] = {"response": response, "cooldown": cooldown,
                                                     "cooldown_type": cooldown_type}
                                    else:
                                        resp = str(entry).strip()
                                        if cmd and resp:
                                            cmds[cmd] = {"response": resp, "cooldown": 0,
                                                         "cooldown_type": "global"}
                                self._config[k] = cmds
```

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all 39 existing tests pass. `test_command_cooldowns.py` tests still fail (routing logic not yet updated).

- [ ] **Step 5: Commit**

```bash
git add twitch_bot.py
git commit -m "feat(commands): update settings API for cooldown data model"
```

---

### Task 3: Rewrite `_route_chat_commands`

**Files:**
- Modify: `twitch_bot.py:2109-2127` (`_route_chat_commands`)

- [ ] **Step 1: Replace `_route_chat_commands` body**

In `twitch_bot.py`, find lines 2109-2127:
```python
    def _route_chat_commands(self, username: str, message: str) -> None:
        with self._config_lock:
            enabled  = self._config.get("chat_commands_enabled", False)
            commands = dict(self._config.get("chat_commands", {}))
            channel  = self._config.get("twitch_channel", "").lower().strip()
        if not enabled or not channel:
            return
        word = message.strip().split()[0].lower() if message.strip() else ""
        if not word.startswith("!"):
            return
        response = commands.get(word)
        if not response:
            return
        args = message.strip()[len(word):].strip()
        response = _apply_placeholders(response, username, channel, word, args)
        irc = self._irc
        if irc:
            irc.say(channel, response[:500])
            self._log(f"[Commands] {username} → {word}")
```

Replace with:
```python
    def _route_chat_commands(self, username: str, message: str) -> None:
        with self._config_lock:
            enabled      = self._config.get("chat_commands_enabled", False)
            commands     = dict(self._config.get("chat_commands", {}))
            channel      = self._config.get("twitch_channel", "").lower().strip()
            list_enabled = self._config.get("cmd_list_enabled", False)
        if not enabled or not channel:
            return
        word = message.strip().split()[0].lower() if message.strip() else ""
        if not word.startswith("!"):
            return
        entry = commands.get(word)
        if entry:
            cooldown      = int(entry.get("cooldown", 0))
            cooldown_type = entry.get("cooldown_type", "global")
            if cooldown > 0:
                now = time.time()
                if cooldown_type == "user":
                    key  = (word, username)
                    last = self._cmd_user_cooldowns.get(key, 0.0)
                    if now - last < cooldown:
                        return
                    self._cmd_user_cooldowns[key] = now
                else:
                    last = self._cmd_global_cooldowns.get(word, 0.0)
                    if now - last < cooldown:
                        return
                    self._cmd_global_cooldowns[word] = now
            response = entry.get("response", "")
            args     = message.strip()[len(word):].strip()
            response = _apply_placeholders(response, username, channel, word, args)
            irc = self._irc
            if irc:
                irc.say(channel, response[:500])
                self._log(f"[Commands] {username} → {word}")
        elif word == "!commands" and list_enabled:
            cmd_list = "Commands: " + ", ".join(sorted(commands.keys()))
            irc = self._irc
            if irc:
                irc.say(channel, cmd_list[:500])
                self._log(f"[Commands] {username} → !commands (auto-list)")
```

- [ ] **Step 2: Run all command cooldown tests**

```bash
python -m pytest tests/test_command_cooldowns.py -v
```

Expected: all 10 tests PASS.

- [ ] **Step 3: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all 49 tests pass (39 existing + 10 new).

- [ ] **Step 4: Commit**

```bash
git add twitch_bot.py
git commit -m "feat(commands): add per-command cooldowns and auto-!commands list"
```

---

### Task 4: Frontend + CLAUDE.md

**Files:**
- Modify: `templates/index.html:446-455` (table header, + Add button area), `templates/index.html:929-931` (`openSettings`), `templates/index.html:1001-1002` (`saveSettings`), `templates/index.html:1063-1087` (`addCmdRow`, `getChatCommands`)
- Modify: `CLAUDE.md` (Custom `!command` responses section)

- [ ] **Step 1: Update the commands table header**

In `templates/index.html`, find lines 447-451:
```html
          <thead>
            <tr>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Command</th>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Response</th>
              <th style="width:32px"></th>
            </tr>
          </thead>
```

Replace with:
```html
          <thead>
            <tr>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Command</th>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Response</th>
              <th style="padding:4px 6px;font-size:11px;color:var(--muted);text-align:center">Cooldown (s)</th>
              <th style="padding:4px 6px;font-size:11px;color:var(--muted);text-align:center">Mode</th>
              <th style="width:32px"></th>
            </tr>
          </thead>
```

- [ ] **Step 2: Add `!commands` toggle checkbox below the + Add button**

In `templates/index.html`, find line 455:
```html
        <button class="btn btn-neutral btn-sm" onclick="addCmdRow()">+ Add command</button>
```

Replace with:
```html
        <button class="btn btn-neutral btn-sm" onclick="addCmdRow()">+ Add command</button>
        <div class="divider"></div>
        <label class="row-check">
          <input type="checkbox" id="s-cmd-list-enabled">
          Enable !commands list
        </label>
        <div class="hint">Viewers can type !commands to see all registered commands. A user-defined !commands entry takes priority.</div>
```

- [ ] **Step 3: Update `addCmdRow` to handle new fields**

In `templates/index.html`, find lines 1063-1072:
```javascript
function addCmdRow(cmd, resp) {
  const tbody = el('cmd-rows');
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td style="padding:2px 4px"><input type="text" placeholder="!hello" style="width:100%" value="${escHtml(cmd||'')}"></td>
    <td style="padding:2px 4px"><input type="text" placeholder="Hey there!" style="width:100%" value="${escHtml(resp||'')}"></td>
    <td style="padding:2px 4px;text-align:center"><button class="btn-icon" onclick="this.closest('tr').remove()">✕</button></td>
  `;
  tbody.appendChild(tr);
}
```

Replace with:
```javascript
function addCmdRow(cmd, entry) {
  const tbody    = el('cmd-rows');
  const tr       = document.createElement('tr');
  const resp     = (entry && typeof entry === 'object') ? (entry.response    || '') : (entry || '');
  const cooldown = (entry && typeof entry === 'object') ? (entry.cooldown    ?? 0)  : 0;
  const cdType   = (entry && typeof entry === 'object') ? (entry.cooldown_type || 'global') : 'global';
  tr.innerHTML = `
    <td style="padding:2px 4px"><input type="text" placeholder="!hello" style="width:100%" value="${escHtml(cmd||'')}"></td>
    <td style="padding:2px 4px"><input type="text" placeholder="Hey there!" style="width:100%" value="${escHtml(resp)}"></td>
    <td style="padding:2px 4px;text-align:center"><input type="number" min="0" style="width:60px" value="${parseInt(cooldown)||0}"></td>
    <td style="padding:2px 4px;text-align:center">
      <select>
        <option value="global"${cdType==='global'?' selected':''}>Global</option>
        <option value="user"${cdType==='user'?' selected':''}>Per-user</option>
      </select>
    </td>
    <td style="padding:2px 4px;text-align:center"><button class="btn-icon" onclick="this.closest('tr').remove()">✕</button></td>
  `;
  tbody.appendChild(tr);
}
```

- [ ] **Step 4: Update `getChatCommands` to read cooldown and mode**

In `templates/index.html`, find lines 1078-1087:
```javascript
function getChatCommands() {
  const cmds = {};
  el('cmd-rows').querySelectorAll('tr').forEach(tr => {
    const inputs = tr.querySelectorAll('input');
    const cmd  = inputs[0].value.trim().toLowerCase();
    const resp = inputs[1].value.trim();
    if (cmd && resp) cmds[cmd] = resp;
  });
  return cmds;
}
```

Replace with:
```javascript
function getChatCommands() {
  const cmds = {};
  el('cmd-rows').querySelectorAll('tr').forEach(tr => {
    const inputs   = tr.querySelectorAll('input');
    const sel      = tr.querySelector('select');
    const cmd      = inputs[0].value.trim().toLowerCase();
    const resp     = inputs[1].value.trim();
    const cooldown = parseInt(inputs[2].value) || 0;
    const cdType   = sel ? sel.value : 'global';
    if (cmd && resp) cmds[cmd] = {response: resp, cooldown: cooldown, cooldown_type: cdType};
  });
  return cmds;
}
```

- [ ] **Step 5: Update `openSettings` to populate new fields**

In `templates/index.html`, find lines 929-931:
```javascript
    el('s-cmd-enabled').checked = !!s.chat_commands_enabled;
    el('cmd-rows').innerHTML = '';
    Object.entries(s.chat_commands || {}).forEach(([cmd, resp]) => addCmdRow(cmd, resp));
```

Replace with:
```javascript
    el('s-cmd-enabled').checked      = !!s.chat_commands_enabled;
    el('s-cmd-list-enabled').checked = !!s.cmd_list_enabled;
    el('cmd-rows').innerHTML = '';
    Object.entries(s.chat_commands || {}).forEach(([cmd, entry]) => addCmdRow(cmd, entry));
```

- [ ] **Step 6: Update `saveSettings` to include `cmd_list_enabled`**

In `templates/index.html`, find lines 1001-1002:
```javascript
    chat_commands_enabled: el('s-cmd-enabled').checked,
    chat_commands:         getChatCommands(),
```

Replace with:
```javascript
    chat_commands_enabled: el('s-cmd-enabled').checked,
    chat_commands:         getChatCommands(),
    cmd_list_enabled:      el('s-cmd-list-enabled').checked,
```

- [ ] **Step 7: Update CLAUDE.md — Custom `!command` responses section**

In `CLAUDE.md`, find lines 84-97 (the body of the `## Custom !command responses` section). Replace this exact block:

```markdown
When `chat_commands_enabled` is `True` and a message starts with a registered `!word`, `_route_chat_commands` posts the configured reply to Twitch chat without invoking the AI. Called from `_dispatch` after the ignore check and before `_route_plays`. Commands are stored as `dict[str, str]` in `chat_commands` (keys normalised to lowercase, auto-prefixed with `!`).

Response strings support placeholder substitution via `_apply_placeholders` (module-level, `twitch_bot.py`):

| Placeholder | Value |
|---|---|
| `%user%` | Twitch login username of the chatter |
| `%channel%` | Twitch channel name |
| `%command%` | The command word (always lowercase, e.g. `!so`) |
| `%args%` | Everything after the command word; empty string if nothing |

Unknown placeholders (e.g. `%usr%`) are left as-is. Responses are truncated to 500 chars **after** substitution. See `Placeholder.md` in the repo root for user-facing docs.

**Note:** If an AI trigger (e.g. every-N counter) fires on the same message as a command match, both responses go to chat. This is by design — the two systems are independent.
```

With:

```markdown
When `chat_commands_enabled` is `True` and a message starts with a registered `!word`, `_route_chat_commands` posts the configured reply to Twitch chat without invoking the AI. Called from `_dispatch` after the ignore check and before `_route_plays`. Commands are stored as `dict[str, dict]` in `chat_commands` (keys normalised to lowercase, auto-prefixed with `!`).

Each command entry has three fields:

| Field | Type | Description |
|---|---|---|
| `response` | `str` | Reply text; placeholder substitution applied; truncated to 500 chars |
| `cooldown` | `int` | Seconds between allowed uses; `0` = no cooldown |
| `cooldown_type` | `str` | `"global"` (channel-wide timer) or `"user"` (per-viewer timer) |

**Migration:** Settings saved with the old `dict[str, str]` format are promoted on load: `"Hey!"` → `{"response": "Hey!", "cooldown": 0, "cooldown_type": "global"}`.

Response strings support placeholder substitution via `_apply_placeholders` (module-level, `twitch_bot.py`):

| Placeholder | Value |
|---|---|
| `%user%` | Twitch login username of the chatter |
| `%channel%` | Twitch channel name |
| `%command%` | The command word (always lowercase, e.g. `!so`) |
| `%args%` | Everything after the command word; empty string if nothing |

Unknown placeholders (e.g. `%usr%`) are left as-is. Responses are truncated to 500 chars **after** substitution. See `Placeholder.md` in the repo root for user-facing docs.

**Cooldown tracking:** Two in-memory dicts on `WebApp` — `_cmd_global_cooldowns: dict[str, float]` and `_cmd_user_cooldowns: dict[tuple[str, str], float]` — store last-fired timestamps. These are never persisted; they reset on bot restart.

**Auto `!commands` list (`cmd_list_enabled`):** When enabled, `_route_chat_commands` responds to `!commands` with an alphabetically sorted, comma-separated list of all registered commands prefixed with `"Commands: "`, truncated to 500 chars. A user-defined `!commands` entry always takes priority.

**Note:** If an AI trigger (e.g. every-N counter) fires on the same message as a command match, both responses go to chat. This is by design — the two systems are independent.
```

- [ ] **Step 8: Run full test suite**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
python -m pytest tests/ -v
```

Expected: all 49 tests pass.

- [ ] **Step 9: Commit**

```bash
git add templates/index.html CLAUDE.md
git commit -m "feat(commands): add cooldown/mode columns and !commands list toggle to UI"
```

---

### Task 5: Final check and push

- [ ] **Step 1: Run full test suite one final time**

```bash
python -m pytest tests/ -v
```

Expected: 49 tests pass, 0 failures.

- [ ] **Step 2: Push**

```bash
git push
```
