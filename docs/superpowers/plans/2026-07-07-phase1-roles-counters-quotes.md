# Phase 1: Roles, Counters, Quotes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a custom role system, persistent counter commands, and a quote system to the Twitch bot.

**Architecture:** Role sets are built per-message in `_dispatch` from IRC badges + `data/roles.json`. Three new routing methods (`_route_role_commands`, `_route_counters`, `_route_quotes`) handle their respective chat commands. Three new Settings tabs manage each feature via dedicated Flask API routes. Data lives in `data/counters.json`, `data/quotes.json`, `data/roles.json`.

**Tech Stack:** Python 3 (threading, json, os, random, datetime), Flask, vanilla JS — no new dependencies.

---

## File Map

| File | Change |
|---|---|
| `twitch_bot.py` | Add 3 locks to `__init__`; add `quote_addquote_role` to defaults/config/save; update `TwitchIRCClient._handle` to pass `badges`; update `_dispatch`; add `_build_user_roles`, `_route_role_commands`, `_route_counters`, `_route_quotes`, `_init_counter_presets`; update `_route_chat_commands`; add API routes; update `_SETTINGS_KEYS`, `api_settings_post` |
| `templates/index.html` | Add Roles/Counters/Quotes tab buttons; add 3 tab pane HTML blocks; add JS functions; update `showTab`, `saveSettings`, `addCmdRow`, `getChatCommands` |
| `tests/test_role_system.py` | New — tests for `_build_user_roles` and `_route_role_commands` |
| `tests/test_counters.py` | New — tests for `_route_counters` |
| `tests/test_quotes.py` | New — tests for `_route_quotes` |
| `tests/test_role_gating.py` | New — tests for `allowed_roles` on `_route_chat_commands` |

---

## Task 1: Foundation — locks, config keys, badge pass-through, `_build_user_roles`

**Files:**
- Modify: `twitch_bot.py:808-832` (IRC handle), `twitch_bot.py:1212-1252` (SETTINGS_DEFAULTS), `twitch_bot.py:1340-1351` (__init__ instance vars), `twitch_bot.py:1272-1335` (_config dict), `twitch_bot.py:1583-1626` (_save_settings), `twitch_bot.py:1807-1826` (_SETTINGS_KEYS), `twitch_bot.py:2285-2310` (_dispatch)
- Create: `tests/test_role_system.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_role_system.py
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && python -m pytest tests/test_role_system.py -v 2>&1 | head -30
```
Expected: `AttributeError: '_build_user_roles'`

- [ ] **Step 3: Add 3 locks + `_init_counter_presets` call to `__init__`**

In `twitch_bot.py` at line 1351 (after `self._stream_cache_ts = 0.0`), add:

```python
        self._roles_lock    = threading.Lock()
        self._counters_lock = threading.Lock()
        self._quotes_lock   = threading.Lock()
```

At the end of `__init__` (before `self._start_services()`), add a call:

```python
        self._init_counter_presets()
```

Add the method to `WebApp` (anywhere before `_start_services`):

```python
    def _init_counter_presets(self) -> None:
        path = os.path.join(self._data_dir, "counters.json")
        if os.path.exists(path):
            return
        presets = {
            "deaths": {"value": 0, "display": "Deaths: {value}", "edit_roles": ["moderator", "broadcaster"]},
            "wins":   {"value": 0, "display": "Wins: {value}",   "edit_roles": ["moderator", "broadcaster"]},
            "losses": {"value": 0, "display": "Losses: {value}", "edit_roles": ["moderator", "broadcaster"]},
        }
        os.makedirs(self._data_dir, exist_ok=True)
        with self._counters_lock:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(presets, f, indent=2)
```

- [ ] **Step 4: Add `quote_addquote_role` to `_SETTINGS_DEFAULTS`**

In `_SETTINGS_DEFAULTS` dict (around line 1251), add after `"ai_context_size": 5,`:

```python
        # ── quotes ─────────────────────────────────────────────────────────────
        "quote_addquote_role": "moderator",
```

- [ ] **Step 5: Add `quote_addquote_role` to `_config` dict in `__init__`**

In the `_config` dict (around line 1334), add after `"ai_context_size":`:

```python
            "quote_addquote_role": settings.get("quote_addquote_role", "moderator"),
```

- [ ] **Step 6: Add `quote_addquote_role` to `_save_settings` and `_SETTINGS_KEYS`**

In `_save_settings` (around line 1622), add after `"ai_context_size"`:

```python
            "quote_addquote_role": c.get("quote_addquote_role", "moderator"),
```

In `_SETTINGS_KEYS` tuple (around line 1825), add after `"ai_context_size",`:

```python
            "quote_addquote_role",
```

- [ ] **Step 7: Update `TwitchIRCClient._handle` to pass badges**

Replace lines 828-832 (the PRIVMSG block):

```python
        m = re.search(r":(\w+)!\w+@\S+\.tmi\.twitch\.tv PRIVMSG #\S+ :(.+)", line)
        if m:
            bits      = int(tags.get("bits", 0) or 0)
            reward_id = tags.get("custom-reward-id", "")
            self.on_message(m.group(1), m.group(2).strip(), bits, reward_id)
```

With:

```python
        m = re.search(r":(\w+)!\w+@\S+\.tmi\.twitch\.tv PRIVMSG #\S+ :(.+)", line)
        if m:
            bits      = int(tags.get("bits", 0) or 0)
            reward_id = tags.get("custom-reward-id", "")
            badges    = tags.get("badges", "")
            self.on_message(m.group(1), m.group(2).strip(), bits, reward_id, badges)
```

- [ ] **Step 8: Update `_dispatch` signature**

Change line 2285-2286:

```python
    def _dispatch(self, username: str, message: str,
                  bits: int = 0, reward_id: str = "") -> None:
```

To:

```python
    def _dispatch(self, username: str, message: str,
                  bits: int = 0, reward_id: str = "", badges: str = "") -> None:
```

- [ ] **Step 9: Implement `_build_user_roles`**

Add this method to `WebApp` (near `_dispatch`, around line 2283):

```python
    def _build_user_roles(self, username: str, badges_str: str) -> set[str]:
        roles: set[str] = {"everyone"}
        _NATIVE = {"broadcaster", "moderator", "vip", "subscriber"}
        for badge in badges_str.split(","):
            name = badge.split("/")[0]
            if name in _NATIVE:
                roles.add(name)
        roles_path = os.path.join(self._data_dir, "roles.json")
        try:
            with self._roles_lock:
                if os.path.exists(roles_path):
                    with open(roles_path, encoding="utf-8") as f:
                        custom = json.load(f)
                    uname = username.lower()
                    for role_name, members in custom.items():
                        if uname in [m.lower() for m in members]:
                            roles.add(role_name)
        except Exception:
            pass
        return roles
```

- [ ] **Step 10: Run tests to verify they pass**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && python -m pytest tests/test_role_system.py -v
```
Expected: all 11 tests PASS

- [ ] **Step 11: Run full test suite to check for regressions**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && python -m pytest tests/ -v 2>&1 | tail -20
```
Expected: all existing tests still pass

- [ ] **Step 12: Commit**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && git add twitch_bot.py tests/test_role_system.py && git commit -m "feat(roles): add _build_user_roles, locks, counter presets, quote_addquote_role config key"
```

---

## Task 2: Role management commands (`!addrole`, `!removerole`, `!roles`)

**Files:**
- Modify: `twitch_bot.py` (add `_route_role_commands`, update `_dispatch`)
- Modify: `tests/test_role_system.py` (add `TestRoleCommands`)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_role_system.py`:

```python
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
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && python -m pytest tests/test_role_system.py::TestRoleCommands -v 2>&1 | head -20
```
Expected: `AttributeError: '_route_role_commands'`

- [ ] **Step 3: Implement `_route_role_commands`**

Add to `WebApp` (near `_build_user_roles`):

```python
    def _route_role_commands(self, username: str, message: str, user_roles: set[str]) -> bool:
        parts = message.strip().split()
        if not parts:
            return False
        cmd = parts[0].lower()
        if cmd not in ("!addrole", "!removerole", "!roles"):
            return False

        with self._config_lock:
            channel = self._config.get("twitch_channel", "").lower().strip()
        irc = self._irc

        is_mod = bool(user_roles & {"moderator", "broadcaster"})

        if cmd == "!addrole":
            if not is_mod or len(parts) < 3:
                return True
            target = parts[1].lower()
            role   = parts[2].lower()
            path   = os.path.join(self._data_dir, "roles.json")
            with self._roles_lock:
                os.makedirs(self._data_dir, exist_ok=True)
                custom: dict = {}
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        custom = json.load(f)
                members = custom.setdefault(role, [])
                if target not in members:
                    members.append(target)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(custom, f, indent=2)
            if irc and channel:
                irc.say(channel, f"Granted '{role}' to {target}.")
            self._log(f"[Roles] {username} → !addrole {target} {role}")

        elif cmd == "!removerole":
            if not is_mod or len(parts) < 3:
                return True
            target = parts[1].lower()
            role   = parts[2].lower()
            path   = os.path.join(self._data_dir, "roles.json")
            with self._roles_lock:
                custom = {}
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        custom = json.load(f)
                if role in custom:
                    custom[role] = [m for m in custom[role] if m != target]
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(custom, f, indent=2)
            if irc and channel:
                irc.say(channel, f"Removed '{role}' from {target}.")
            self._log(f"[Roles] {username} → !removerole {target} {role}")

        elif cmd == "!roles":
            if len(parts) < 2:
                return True
            target = parts[1].lower()
            found: set[str] = {"everyone"}
            path = os.path.join(self._data_dir, "roles.json")
            with self._roles_lock:
                if os.path.exists(path):
                    try:
                        with open(path, encoding="utf-8") as f:
                            custom = json.load(f)
                        for r, members in custom.items():
                            if target in [m.lower() for m in members]:
                                found.add(r)
                    except Exception:
                        pass
            if irc and channel:
                irc.say(channel, f"{target} roles: {', '.join(sorted(found))}")

        return True
```

- [ ] **Step 4: Wire into `_dispatch`**

In `_dispatch`, after building history and before routing, add the role-set build and role command routing. Replace:

```python
        self._route_chat_commands(username, message)
        self._route_plays(username, message)
        self._route_ai(username, message, bits, reward_id)
```

With:

```python
        user_roles = self._build_user_roles(username, badges)
        self._route_role_commands(username, message, user_roles)
        handled = self._route_counters(username, message, user_roles)
        handled = handled or self._route_quotes(username, message, user_roles)
        if not handled:
            self._route_chat_commands(username, message, user_roles)
        self._route_plays(username, message)
        self._route_ai(username, message, bits, reward_id)
```

Note: `_route_counters` and `_route_quotes` don't exist yet — they will be added in Tasks 4 and 5. Add stub methods now so `_dispatch` doesn't crash:

```python
    def _route_counters(self, username: str, message: str, user_roles: set[str]) -> bool:
        return False  # implemented in Task 4

    def _route_quotes(self, username: str, message: str, user_roles: set[str]) -> bool:
        return False  # implemented in Task 5
```

- [ ] **Step 5: Run tests**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && python -m pytest tests/test_role_system.py -v && python -m pytest tests/ -v 2>&1 | tail -10
```
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && git add twitch_bot.py tests/test_role_system.py && git commit -m "feat(roles): add _route_role_commands (!addrole, !removerole, !roles)"
```

---

## Task 3: Command role gating (`allowed_roles` on existing `!commands`)

**Files:**
- Modify: `twitch_bot.py` (`_route_chat_commands`, `__init__` config loading, `api_settings_post`)
- Create: `tests/test_role_gating.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_role_gating.py
"""Tests for allowed_roles gating on _route_chat_commands."""
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


def _make_app(commands: dict) -> twitch_bot.WebApp:
    app = object.__new__(twitch_bot.WebApp)
    app._config_lock = threading.Lock()
    app._config = {
        "chat_commands_enabled": True,
        "chat_commands": commands,
        "twitch_channel": "testchannel",
        "cmd_list_enabled": False,
    }
    app._irc = MagicMock()
    app._log = lambda msg: None
    app._cmd_global_cooldowns = {}
    app._cmd_user_cooldowns   = {}
    app._cmd_use_counts       = {}
    app._data_dir             = os.path.join(os.path.dirname(__file__), "..", "data")
    app._stream_cache         = {}
    app._stream_cache_ts      = 0.0
    return app


_CMD_MOD_ONLY = {"response": "Mod response!", "cooldown": 0,
                 "cooldown_type": "global", "allowed_roles": ["moderator"]}
_CMD_SUB_ONLY = {"response": "Sub response!", "cooldown": 0,
                 "cooldown_type": "global", "allowed_roles": ["subscriber"]}
_CMD_OPEN     = {"response": "Open!",         "cooldown": 0,
                 "cooldown_type": "global", "allowed_roles": []}


class TestCommandRoleGating(unittest.TestCase):
    def test_mod_only_fires_for_mod(self):
        app = _make_app({"!secret": _CMD_MOD_ONLY})
        app._route_chat_commands("mod1", "!secret", {"moderator", "everyone"})
        app._irc.say.assert_called_once()

    def test_mod_only_blocked_for_viewer(self):
        app = _make_app({"!secret": _CMD_MOD_ONLY})
        app._route_chat_commands("viewer1", "!secret", {"everyone"})
        app._irc.say.assert_not_called()

    def test_mod_only_fires_for_broadcaster(self):
        app = _make_app({"!secret": _CMD_MOD_ONLY})
        app._route_chat_commands("streamer", "!secret", {"broadcaster", "everyone"})
        app._irc.say.assert_not_called()  # broadcaster not in allowed_roles: ["moderator"]

    def test_broadcaster_in_allowed_roles_fires(self):
        cmd = {"response": "hi", "cooldown": 0, "cooldown_type": "global",
               "allowed_roles": ["broadcaster"]}
        app = _make_app({"!cmd": cmd})
        app._route_chat_commands("streamer", "!cmd", {"broadcaster", "everyone"})
        app._irc.say.assert_called_once()

    def test_open_command_fires_for_everyone(self):
        app = _make_app({"!hello": _CMD_OPEN})
        app._route_chat_commands("viewer1", "!hello", {"everyone"})
        app._irc.say.assert_called_once()

    def test_no_user_roles_bypasses_check(self):
        """user_roles=None means old callers — no gating applied."""
        app = _make_app({"!secret": _CMD_MOD_ONLY})
        app._route_chat_commands("viewer1", "!secret", None)
        app._irc.say.assert_called_once()

    def test_sub_command_fires_for_sub(self):
        app = _make_app({"!sub": _CMD_SUB_ONLY})
        app._route_chat_commands("sub1", "!sub", {"subscriber", "everyone"})
        app._irc.say.assert_called_once()

    def test_sub_command_blocked_for_non_sub(self):
        app = _make_app({"!sub": _CMD_SUB_ONLY})
        app._route_chat_commands("viewer1", "!sub", {"everyone"})
        app._irc.say.assert_not_called()

    def test_custom_role_gates_command(self):
        cmd = {"response": "hi", "cooldown": 0, "cooldown_type": "global",
               "allowed_roles": ["trusted"]}
        app = _make_app({"!vip": cmd})
        app._route_chat_commands("viewer1", "!vip", {"trusted", "everyone"})
        app._irc.say.assert_called_once()
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && python -m pytest tests/test_role_gating.py -v 2>&1 | head -20
```
Expected: most tests fail because `_route_chat_commands` doesn't accept `user_roles` yet

- [ ] **Step 3: Update `_route_chat_commands` signature and add role check**

Change the method signature at line 2345 from:
```python
    def _route_chat_commands(self, username: str, message: str) -> None:
```
To:
```python
    def _route_chat_commands(self, username: str, message: str,
                             user_roles: set[str] | None = None) -> None:
```

After `entry = commands.get(word)` and before the cooldown check, add:

```python
        if entry:
            allowed = entry.get("allowed_roles", [])
            if allowed and user_roles is not None and not (user_roles & set(allowed)):
                return
```

The full updated entry block becomes:

```python
        entry = commands.get(word)
        if entry:
            allowed = entry.get("allowed_roles", [])
            if allowed and user_roles is not None and not (user_roles & set(allowed)):
                return
            try:
                cooldown = int(entry.get("cooldown", 0) or 0)
            # ... rest unchanged
```

- [ ] **Step 4: Inject `allowed_roles: []` for existing commands in `__init__`**

In `__init__` where `_config["chat_commands"]` is built (around line 1326), update to:

```python
            "chat_commands": {
                k: (
                    {**v, "allowed_roles": v.get("allowed_roles", [])}
                    if isinstance(v, dict)
                    else {"response": v, "cooldown": 0, "cooldown_type": "global", "allowed_roles": []}
                )
                for k, v in settings.get("chat_commands", {}).items()
            },
```

- [ ] **Step 5: Update `api_settings_post` to preserve `allowed_roles` in chat_commands**

In the `chat_commands` parsing block of `api_settings_post` (around line 1876), change:
```python
                                        cmds[cmd] = {"response": response, "cooldown": cooldown,
                                                     "cooldown_type": cooldown_type}
```
To:
```python
                                        raw_roles = entry.get("allowed_roles", [])
                                        if isinstance(raw_roles, list):
                                            allowed_roles = [str(r).strip().lower() for r in raw_roles if r]
                                        elif isinstance(raw_roles, str) and raw_roles.strip():
                                            allowed_roles = [r.strip().lower() for r in raw_roles.split(",") if r.strip()]
                                        else:
                                            allowed_roles = []
                                        cmds[cmd] = {"response": response, "cooldown": cooldown,
                                                     "cooldown_type": cooldown_type, "allowed_roles": allowed_roles}
```

Also update the legacy `str` entry branch just below:
```python
                                    else:
                                        resp = str(entry).strip()
                                        if cmd and resp:
                                            cmds[cmd] = {"response": resp, "cooldown": 0,
                                                         "cooldown_type": "global", "allowed_roles": []}
```

- [ ] **Step 6: Run tests**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && python -m pytest tests/test_role_gating.py tests/test_command_cooldowns.py -v
```
Expected: all pass

- [ ] **Step 7: Commit**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && git add twitch_bot.py tests/test_role_gating.py && git commit -m "feat(roles): add allowed_roles gating to _route_chat_commands"
```

---

## Task 4: Counter routing

**Files:**
- Modify: `twitch_bot.py` (replace stub `_route_counters`)
- Create: `tests/test_counters.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_counters.py
"""Tests for _route_counters."""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


def _make_app(tmpdir: str) -> twitch_bot.WebApp:
    app = object.__new__(twitch_bot.WebApp)
    app._config_lock   = threading.Lock()
    app._config        = {"twitch_channel": "testchannel"}
    app._counters_lock = threading.Lock()
    app._data_dir      = tmpdir
    app._irc           = MagicMock()
    app._log           = lambda msg: None
    return app


def _write_counters(tmpdir: str, data: dict) -> None:
    with open(os.path.join(tmpdir, "counters.json"), "w") as f:
        json.dump(data, f)


def _read_counters(tmpdir: str) -> dict:
    with open(os.path.join(tmpdir, "counters.json")) as f:
        return json.load(f)


_MOD = {"moderator", "everyone"}
_ALL = {"everyone"}

_BASE_COUNTERS = {
    "deaths": {"value": 5, "display": "Deaths: {value}", "edit_roles": ["moderator"]}
}


class TestRouteCounters(unittest.TestCase):
    def test_display_counter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, _BASE_COUNTERS)
            app = _make_app(tmpdir)
            result = app._route_counters("viewer1", "!deaths", _ALL)
            self.assertTrue(result)
            app._irc.say.assert_called_once()
            _, reply = app._irc.say.call_args[0]
            self.assertIn("5", reply)

    def test_increment_as_mod(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, _BASE_COUNTERS)
            app = _make_app(tmpdir)
            app._route_counters("mod1", "!deaths +1", _MOD)
            self.assertEqual(_read_counters(tmpdir)["deaths"]["value"], 6)

    def test_increment_blocked_for_viewer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, _BASE_COUNTERS)
            app = _make_app(tmpdir)
            app._route_counters("viewer1", "!deaths +1", _ALL)
            self.assertEqual(_read_counters(tmpdir)["deaths"]["value"], 5)

    def test_decrement_floors_at_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            counters = {"deaths": {"value": 0, "display": "Deaths: {value}", "edit_roles": ["moderator"]}}
            _write_counters(tmpdir, counters)
            app = _make_app(tmpdir)
            app._route_counters("mod1", "!deaths -1", _MOD)
            self.assertEqual(_read_counters(tmpdir)["deaths"]["value"], 0)

    def test_set_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, _BASE_COUNTERS)
            app = _make_app(tmpdir)
            app._route_counters("mod1", "!deaths set 42", _MOD)
            self.assertEqual(_read_counters(tmpdir)["deaths"]["value"], 42)

    def test_reset_counter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, _BASE_COUNTERS)
            app = _make_app(tmpdir)
            app._route_counters("mod1", "!deaths reset", _MOD)
            self.assertEqual(_read_counters(tmpdir)["deaths"]["value"], 0)

    def test_addcounter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, {})
            app = _make_app(tmpdir)
            app._route_counters("mod1", "!addcounter hype", _MOD)
            self.assertIn("hype", _read_counters(tmpdir))

    def test_delcounter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, _BASE_COUNTERS)
            app = _make_app(tmpdir)
            app._route_counters("mod1", "!delcounter deaths", _MOD)
            self.assertNotIn("deaths", _read_counters(tmpdir))

    def test_unknown_counter_returns_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, {})
            app = _make_app(tmpdir)
            result = app._route_counters("viewer1", "!deaths", _ALL)
            self.assertFalse(result)

    def test_non_counter_message_returns_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_counters(tmpdir, _BASE_COUNTERS)
            app = _make_app(tmpdir)
            result = app._route_counters("viewer1", "hello world", _ALL)
            self.assertFalse(result)

    def test_display_format_applied(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            counters = {"deaths": {"value": 3, "display": "💀 {value} deaths!", "edit_roles": []}}
            _write_counters(tmpdir, counters)
            app = _make_app(tmpdir)
            app._route_counters("viewer1", "!deaths", _ALL)
            _, reply = app._irc.say.call_args[0]
            self.assertEqual(reply, "💀 3 deaths!")
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && python -m pytest tests/test_counters.py -v 2>&1 | head -20
```
Expected: tests fail (stub returns False always)

- [ ] **Step 3: Replace stub `_route_counters` with full implementation**

Replace the stub method with:

```python
    def _route_counters(self, username: str, message: str, user_roles: set[str]) -> bool:
        parts = message.strip().split()
        if not parts:
            return False
        word = parts[0].lower()
        if not word.startswith("!"):
            return False

        with self._config_lock:
            channel = self._config.get("twitch_channel", "").lower().strip()
        irc = self._irc
        path = os.path.join(self._data_dir, "counters.json")

        # ── management commands ────────────────────────────────────────────────
        if word == "!addcounter":
            if not (user_roles & {"moderator", "broadcaster"}) or len(parts) < 2:
                return True
            name = parts[1].lower()
            with self._counters_lock:
                os.makedirs(self._data_dir, exist_ok=True)
                counters: dict = {}
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        counters = json.load(f)
                if name not in counters:
                    counters[name] = {
                        "value": 0,
                        "display": f"{name.title()}: {{value}}",
                        "edit_roles": ["moderator", "broadcaster"],
                    }
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(counters, f, indent=2)
            if irc and channel:
                irc.say(channel, f"Counter '!{name}' created.")
            self._log(f"[Counters] {username} created !{name}")
            return True

        if word == "!delcounter":
            if not (user_roles & {"moderator", "broadcaster"}) or len(parts) < 2:
                return True
            name = parts[1].lower()
            with self._counters_lock:
                counters = {}
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        counters = json.load(f)
                if name in counters:
                    del counters[name]
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(counters, f, indent=2)
            if irc and channel:
                irc.say(channel, f"Counter '!{name}' deleted.")
            self._log(f"[Counters] {username} deleted !{name}")
            return True

        # ── counter operation ──────────────────────────────────────────────────
        counter_name = word[1:]
        display_text: str | None = None

        with self._counters_lock:
            counters = {}
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    counters = json.load(f)
            if counter_name not in counters:
                return False

            entry      = counters[counter_name]
            edit_roles = set(entry.get("edit_roles", []))
            subword    = parts[1].lower() if len(parts) > 1 else ""
            changed    = False

            if subword == "+1":
                if user_roles & edit_roles:
                    entry["value"] = max(0, entry["value"] + 1)
                    changed = True
            elif subword == "-1":
                if user_roles & edit_roles:
                    entry["value"] = max(0, entry["value"] - 1)
                    changed = True
            elif subword == "set" and len(parts) > 2:
                if user_roles & {"moderator", "broadcaster"}:
                    try:
                        entry["value"] = int(parts[2])
                        changed = True
                    except ValueError:
                        pass
            elif subword == "reset":
                if user_roles & {"moderator", "broadcaster"}:
                    entry["value"] = 0
                    changed = True

            if changed:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(counters, f, indent=2)

            tmpl = entry.get("display", f"{counter_name.title()}: {{value}}")
            display_text = tmpl.replace("{value}", str(entry["value"]))

        if display_text is not None and irc and channel:
            irc.say(channel, display_text)
            self._log(f"[Counters] {username} → !{counter_name}")
        return True
```

- [ ] **Step 4: Run tests**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && python -m pytest tests/test_counters.py -v && python -m pytest tests/ -v 2>&1 | tail -10
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && git add twitch_bot.py tests/test_counters.py && git commit -m "feat(counters): add _route_counters with increment/decrement/set/reset/add/del"
```

---

## Task 5: Quote routing

**Files:**
- Modify: `twitch_bot.py` (replace stub `_route_quotes`)
- Create: `tests/test_quotes.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_quotes.py
"""Tests for _route_quotes."""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


def _make_app(tmpdir: str, addquote_role: str = "moderator") -> twitch_bot.WebApp:
    app = object.__new__(twitch_bot.WebApp)
    app._config_lock  = threading.Lock()
    app._config       = {
        "twitch_channel":      "testchannel",
        "quote_addquote_role": addquote_role,
    }
    app._quotes_lock  = threading.Lock()
    app._data_dir     = tmpdir
    app._irc          = MagicMock()
    app._log          = lambda msg: None
    return app


def _write_quotes(tmpdir: str, quotes: list) -> None:
    with open(os.path.join(tmpdir, "quotes.json"), "w") as f:
        json.dump(quotes, f)


def _read_quotes(tmpdir: str) -> list:
    with open(os.path.join(tmpdir, "quotes.json")) as f:
        return json.load(f)


_MOD = {"moderator", "everyone"}
_ALL = {"everyone"}

_SAMPLE_QUOTES = [
    {"id": 1, "text": "First quote!", "author": "streamer", "added_by": "mod1", "timestamp": "2026-07-07T12:00:00"},
    {"id": 2, "text": "Second quote!", "author": "streamer", "added_by": "mod2", "timestamp": "2026-07-07T13:00:00"},
]


class TestRouteQuotes(unittest.TestCase):
    def test_random_quote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, _SAMPLE_QUOTES)
            app = _make_app(tmpdir)
            result = app._route_quotes("viewer1", "!quote", _ALL)
            self.assertTrue(result)
            app._irc.say.assert_called_once()
            _, reply = app._irc.say.call_args[0]
            self.assertIn("[#", reply)

    def test_quote_by_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, _SAMPLE_QUOTES)
            app = _make_app(tmpdir)
            app._route_quotes("viewer1", "!quote 2", _ALL)
            _, reply = app._irc.say.call_args[0]
            self.assertIn("Second quote!", reply)
            self.assertIn("[#2]", reply)

    def test_quote_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, _SAMPLE_QUOTES)
            app = _make_app(tmpdir)
            app._route_quotes("viewer1", "!quote 99", _ALL)
            _, reply = app._irc.say.call_args[0]
            self.assertIn("not found", reply.lower())

    def test_quotecount(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, _SAMPLE_QUOTES)
            app = _make_app(tmpdir)
            app._route_quotes("viewer1", "!quotecount", _ALL)
            _, reply = app._irc.say.call_args[0]
            self.assertIn("2", reply)

    def test_addquote_as_mod(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, [])
            app = _make_app(tmpdir)
            app._route_quotes("mod1", "!addquote This is a quote", _MOD)
            quotes = _read_quotes(tmpdir)
            self.assertEqual(len(quotes), 1)
            self.assertEqual(quotes[0]["text"], "This is a quote")
            self.assertEqual(quotes[0]["added_by"], "mod1")

    def test_addquote_blocked_for_viewer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, [])
            app = _make_app(tmpdir)
            app._route_quotes("viewer1", "!addquote Sneaky", _ALL)
            self.assertEqual(_read_quotes(tmpdir), [])

    def test_addquote_custom_role(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, [])
            app = _make_app(tmpdir, addquote_role="trusted")
            app._route_quotes("viewer1", "!addquote Nice quote", {"trusted", "everyone"})
            self.assertEqual(len(_read_quotes(tmpdir)), 1)

    def test_addquote_auto_increments_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, _SAMPLE_QUOTES)
            app = _make_app(tmpdir)
            app._route_quotes("mod1", "!addquote Third one", _MOD)
            quotes = _read_quotes(tmpdir)
            self.assertEqual(quotes[-1]["id"], 3)

    def test_delquote_as_mod(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, _SAMPLE_QUOTES)
            app = _make_app(tmpdir)
            app._route_quotes("mod1", "!delquote 1", _MOD)
            quotes = _read_quotes(tmpdir)
            ids = [q["id"] for q in quotes]
            self.assertNotIn(1, ids)

    def test_delquote_blocked_for_viewer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, _SAMPLE_QUOTES)
            app = _make_app(tmpdir)
            app._route_quotes("viewer1", "!delquote 1", _ALL)
            self.assertEqual(len(_read_quotes(tmpdir)), 2)

    def test_non_quote_command_returns_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, [])
            app = _make_app(tmpdir)
            result = app._route_quotes("viewer1", "!hello", _ALL)
            self.assertFalse(result)

    def test_empty_quotes_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_quotes(tmpdir, [])
            app = _make_app(tmpdir)
            app._route_quotes("viewer1", "!quote", _ALL)
            app._irc.say.assert_called_once()
            _, reply = app._irc.say.call_args[0]
            self.assertIn("No quotes", reply)
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && python -m pytest tests/test_quotes.py -v 2>&1 | head -20
```
Expected: tests fail (stub returns False)

- [ ] **Step 3: Replace stub `_route_quotes` with full implementation**

Replace the stub method with:

```python
    def _route_quotes(self, username: str, message: str, user_roles: set[str]) -> bool:
        parts = message.strip().split()
        if not parts:
            return False
        word = parts[0].lower()
        if word not in ("!quote", "!quotecount", "!addquote", "!delquote"):
            return False

        with self._config_lock:
            channel      = self._config.get("twitch_channel", "").lower().strip()
            addquote_role = self._config.get("quote_addquote_role", "moderator")
        irc  = self._irc
        path = os.path.join(self._data_dir, "quotes.json")

        def _load() -> list:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            return []

        def _save(quotes: list) -> None:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(quotes, f, indent=2)

        def _fmt(q: dict) -> str:
            date = q.get("timestamp", "")[:10]
            return f'[#{q["id"]}] {q["text"]} — {q["author"]} ({date})'

        if word == "!quotecount":
            with self._quotes_lock:
                count = len(_load())
            if irc and channel:
                irc.say(channel, f"Total quotes: {count}")
            return True

        if word == "!addquote":
            if not (user_roles & {addquote_role, "broadcaster"}):
                return True
            text = message.strip()[len("!addquote"):].strip()
            if not text:
                return True
            with self._config_lock:
                author = self._config.get("twitch_channel", "").lower().strip()
            with self._quotes_lock:
                quotes   = _load()
                next_id  = max((q["id"] for q in quotes), default=0) + 1
                quotes.append({
                    "id":        next_id,
                    "text":      text,
                    "author":    author,
                    "added_by":  username,
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                })
                _save(quotes)
            if irc and channel:
                irc.say(channel, f"Quote #{next_id} added!")
            self._log(f"[Quotes] {username} added #{next_id}")
            return True

        if word == "!delquote":
            if not (user_roles & {"moderator", "broadcaster"}) or len(parts) < 2:
                return True
            try:
                target_id = int(parts[1])
            except ValueError:
                return True
            with self._quotes_lock:
                quotes = _load()
                before = len(quotes)
                quotes = [q for q in quotes if q["id"] != target_id]
                if len(quotes) < before:
                    _save(quotes)
                    msg = f"Quote #{target_id} deleted."
                else:
                    msg = f"Quote #{target_id} not found."
            if irc and channel:
                irc.say(channel, msg)
            return True

        # !quote [id]
        with self._quotes_lock:
            quotes = _load()
        if not quotes:
            if irc and channel:
                irc.say(channel, "No quotes yet! Add one with !addquote <text>")
            return True
        if len(parts) > 1:
            try:
                target_id = int(parts[1])
                match = next((q for q in quotes if q["id"] == target_id), None)
                reply = _fmt(match) if match else f"Quote #{target_id} not found."
            except ValueError:
                reply = random.choice(quotes)
                reply = _fmt(reply)
        else:
            reply = _fmt(random.choice(quotes))
        if irc and channel:
            irc.say(channel, reply)
        return True
```

- [ ] **Step 4: Run tests**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && python -m pytest tests/test_quotes.py -v && python -m pytest tests/ -v 2>&1 | tail -10
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && git add twitch_bot.py tests/test_quotes.py && git commit -m "feat(quotes): add _route_quotes (!quote, !addquote, !delquote, !quotecount)"
```

---

## Task 6: API routes for Roles, Counters, and Quotes

**Files:**
- Modify: `twitch_bot.py` (`_register_routes`, add routes after the data files section ~line 2112)

All routes follow the existing `api_datafiles_*` pattern: acquire lock, load, mutate, save, return JSON.

- [ ] **Step 1: Add all API routes**

Add these routes inside `_register_routes`, after the `api_datafile_delete` route:

```python
        # ── roles ─────────────────────────────────────────────────────────────

        @app.route("/api/roles")
        def api_roles_get():
            path = os.path.join(self._data_dir, "roles.json")
            with self._roles_lock:
                data = {}
                if os.path.exists(path):
                    try:
                        with open(path, encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception:
                        pass
            return _flask.jsonify({"roles": data})

        @app.route("/api/roles/<role>", methods=["DELETE"])
        def api_roles_delete(role: str):
            path = os.path.join(self._data_dir, "roles.json")
            with self._roles_lock:
                data = {}
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                data.pop(role, None)
                os.makedirs(self._data_dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            return _flask.jsonify({"ok": True})

        @app.route("/api/roles/<role>/members", methods=["POST"])
        def api_roles_add_member(role: str):
            body = _flask.request.get_json(force=True, silent=True) or {}
            user = str(body.get("user", "")).strip().lower()
            if not user:
                return _flask.jsonify({"error": "user required"}), 400
            path = os.path.join(self._data_dir, "roles.json")
            with self._roles_lock:
                data = {}
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                members = data.setdefault(role, [])
                if user not in members:
                    members.append(user)
                os.makedirs(self._data_dir, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            return _flask.jsonify({"ok": True})

        @app.route("/api/roles/<role>/members/<user>", methods=["DELETE"])
        def api_roles_remove_member(role: str, user: str):
            path = os.path.join(self._data_dir, "roles.json")
            with self._roles_lock:
                data = {}
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                if role in data:
                    data[role] = [m for m in data[role] if m != user.lower()]
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            return _flask.jsonify({"ok": True})

        # ── counters ───────────────────────────────────────────────────────────

        @app.route("/api/counters")
        def api_counters_get():
            path = os.path.join(self._data_dir, "counters.json")
            with self._counters_lock:
                data = {}
                if os.path.exists(path):
                    try:
                        with open(path, encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception:
                        pass
            return _flask.jsonify({"counters": data})

        @app.route("/api/counters", methods=["POST"])
        def api_counters_create():
            body = _flask.request.get_json(force=True, silent=True) or {}
            name = str(body.get("name", "")).strip().lower()
            if not name:
                return _flask.jsonify({"error": "name required"}), 400
            path = os.path.join(self._data_dir, "counters.json")
            with self._counters_lock:
                os.makedirs(self._data_dir, exist_ok=True)
                data = {}
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                if name not in data:
                    data[name] = {
                        "value": 0,
                        "display": body.get("display", f"{name.title()}: {{value}}"),
                        "edit_roles": body.get("edit_roles", ["moderator", "broadcaster"]),
                    }
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
            return _flask.jsonify({"ok": True})

        @app.route("/api/counters/<name>", methods=["PATCH"])
        def api_counters_update(name: str):
            body = _flask.request.get_json(force=True, silent=True) or {}
            path = os.path.join(self._data_dir, "counters.json")
            with self._counters_lock:
                data = {}
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                if name not in data:
                    return _flask.jsonify({"error": "not found"}), 404
                if "value" in body:
                    try:
                        data[name]["value"] = int(body["value"])
                    except (ValueError, TypeError):
                        pass
                if "display" in body:
                    data[name]["display"] = str(body["display"])
                if "edit_roles" in body and isinstance(body["edit_roles"], list):
                    data[name]["edit_roles"] = body["edit_roles"]
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            return _flask.jsonify({"ok": True})

        @app.route("/api/counters/<name>", methods=["DELETE"])
        def api_counters_delete(name: str):
            path = os.path.join(self._data_dir, "counters.json")
            with self._counters_lock:
                data = {}
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                data.pop(name, None)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            return _flask.jsonify({"ok": True})

        # ── quotes ─────────────────────────────────────────────────────────────

        @app.route("/api/quotes")
        def api_quotes_get():
            q = _flask.request.args.get("q", "").lower()
            path = os.path.join(self._data_dir, "quotes.json")
            with self._quotes_lock:
                quotes = []
                if os.path.exists(path):
                    try:
                        with open(path, encoding="utf-8") as f:
                            quotes = json.load(f)
                    except Exception:
                        pass
            if q:
                quotes = [x for x in quotes
                          if q in x.get("text", "").lower()
                          or q in x.get("author", "").lower()
                          or q in x.get("added_by", "").lower()]
            return _flask.jsonify({"quotes": quotes})

        @app.route("/api/quotes/<int:quote_id>", methods=["DELETE"])
        def api_quotes_delete(quote_id: int):
            path = os.path.join(self._data_dir, "quotes.json")
            with self._quotes_lock:
                quotes = []
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        quotes = json.load(f)
                quotes = [q for q in quotes if q["id"] != quote_id]
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(quotes, f, indent=2)
            return _flask.jsonify({"ok": True})
```

- [ ] **Step 2: Run full test suite**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && python -m pytest tests/ -v 2>&1 | tail -15
```
Expected: all pass (API routes are not unit-tested here; covered by integration)

- [ ] **Step 3: Commit**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && git add twitch_bot.py && git commit -m "feat: add API routes for roles, counters, and quotes"
```

---

## Task 7: Web UI — Roles, Counters, and Quotes tabs

**Files:**
- Modify: `templates/index.html`

This task adds three Settings tabs and updates the Commands tab to show `allowed_roles`.

- [ ] **Step 1: Add tab buttons to the tab-bar**

Find the tab-bar (around line 245). After `<button class="tab" onclick="showTab('schedule')">Schedule</button>`, add:

```html
      <button class="tab"        onclick="showTab('roles')">Roles</button>
      <button class="tab"        onclick="showTab('counters')">Counters</button>
      <button class="tab"        onclick="showTab('quotes')">Quotes</button>
```

- [ ] **Step 2: Add HTML for the Roles tab**

After the closing `</div>` of `tab-schedule` (around line 512), add:

```html
      <!-- Roles tab -->
      <div id="tab-roles" class="tab-pane">
        <div class="section-lbl">Custom Roles</div>
        <div class="hint">Assign custom roles to viewers. Twitch native roles (broadcaster, moderator, vip, subscriber) are automatic. Use <strong>!addrole &lt;user&gt; &lt;role&gt;</strong> in chat or manage here.</div>
        <div class="divider"></div>
        <div style="display:flex;gap:8px;margin-bottom:10px">
          <input id="new-role-name" type="text" placeholder="Role name (e.g. trusted)" style="flex:1">
          <button class="btn btn-green btn-sm" onclick="createRole()">+ Create role</button>
        </div>
        <div id="roles-list"></div>
      </div>

      <!-- Counters tab -->
      <div id="tab-counters" class="tab-pane">
        <div class="section-lbl">Counters</div>
        <div class="hint">Persistent counters for chat. Viewers type <strong>!deaths</strong> to see the value; mods type <strong>!deaths +1</strong> or <strong>!deaths -1</strong> to change it.</div>
        <div class="divider"></div>
        <table id="counters-table" style="width:100%;border-collapse:collapse;margin-bottom:8px">
          <thead>
            <tr>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Command</th>
              <th style="text-align:center;padding:4px 6px;font-size:11px;color:var(--muted)">Value</th>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Display format</th>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Edit roles</th>
              <th style="width:32px"></th>
            </tr>
          </thead>
          <tbody id="counters-rows"></tbody>
        </table>
        <div style="display:flex;gap:8px;margin-top:4px">
          <input id="new-counter-name" type="text" placeholder="Counter name (e.g. hype)" style="flex:1">
          <button class="btn btn-green btn-sm" onclick="addCounter()">+ Add counter</button>
        </div>
      </div>

      <!-- Quotes tab -->
      <div id="tab-quotes" class="tab-pane">
        <div class="section-lbl">Quotes</div>
        <div class="hint">Viewers type <strong>!quote</strong> for a random quote or <strong>!quote #</strong> for a specific one. Authorized users add quotes with <strong>!addquote &lt;text&gt;</strong>.</div>
        <div class="divider"></div>
        <div style="display:flex;gap:8px;margin-bottom:8px;align-items:center">
          <label style="font-size:13px;white-space:nowrap">!addquote role:</label>
          <select id="s-quote-addquote-role" style="flex:0 0 140px">
            <option value="moderator">moderator</option>
            <option value="vip">vip</option>
            <option value="subscriber">subscriber</option>
            <option value="everyone">everyone</option>
          </select>
          <input id="quote-search" type="text" placeholder="Search quotes…" style="flex:1" oninput="loadQuotes()">
        </div>
        <table id="quotes-table" style="width:100%;border-collapse:collapse;margin-bottom:8px">
          <thead>
            <tr>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted);width:36px">#</th>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Quote</th>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Added by</th>
              <th style="padding:4px 6px;font-size:11px;color:var(--muted);width:80px">Date</th>
              <th style="width:32px"></th>
            </tr>
          </thead>
          <tbody id="quotes-rows"></tbody>
        </table>
        <div id="quotes-empty" style="display:none;color:var(--muted);font-size:13px;padding:8px 0">No quotes yet.</div>
      </div>
```

- [ ] **Step 3: Update the Commands tab header to add Roles column**

Find the `<thead>` in `tab-commands` (around line 447). Replace:

```html
            <tr>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Command</th>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Response</th>
              <th style="padding:4px 6px;font-size:11px;color:var(--muted);text-align:center">Cooldown (s)</th>
              <th style="padding:4px 6px;font-size:11px;color:var(--muted);text-align:center">Mode</th>
              <th style="width:32px"></th>
            </tr>
```

With:

```html
            <tr>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Command</th>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Response</th>
              <th style="padding:4px 6px;font-size:11px;color:var(--muted);text-align:center">Cooldown (s)</th>
              <th style="padding:4px 6px;font-size:11px;color:var(--muted);text-align:center">Mode</th>
              <th style="padding:4px 6px;font-size:11px;color:var(--muted)">Req. role</th>
              <th style="width:32px"></th>
            </tr>
```

- [ ] **Step 4: Update `addCmdRow` to include allowed_roles field**

Find `addCmdRow` (around line 1104). Replace the entire function with:

```javascript
function addCmdRow(cmd, entry) {
  const tbody      = el('cmd-rows');
  const tr         = document.createElement('tr');
  const resp       = (entry && typeof entry === 'object') ? (entry.response     || '') : (entry || '');
  const cooldown   = (entry && typeof entry === 'object') ? (entry.cooldown     ?? 0)  : 0;
  const cdType     = (entry && typeof entry === 'object') ? (entry.cooldown_type || 'global') : 'global';
  const rolesArr   = (entry && typeof entry === 'object' && Array.isArray(entry.allowed_roles)) ? entry.allowed_roles : [];
  const rolesStr   = rolesArr.join(', ');
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
    <td style="padding:2px 4px"><input type="text" placeholder="everyone" style="width:100%" value="${escHtml(rolesStr)}" title="Comma-separated roles (empty = everyone)"></td>
    <td style="padding:2px 4px;text-align:center"><button class="btn-icon" onclick="this.closest('tr').remove()">✕</button></td>
  `;
  tbody.appendChild(tr);
}
```

- [ ] **Step 5: Update `getChatCommands` to include `allowed_roles`**

Find `getChatCommands` (around line 1186). Replace with:

```javascript
function getChatCommands() {
  const cmds = {};
  el('cmd-rows').querySelectorAll('tr').forEach(tr => {
    const inputs = tr.querySelectorAll('input, select');
    const cmd    = inputs[0].value.trim().toLowerCase();
    const resp   = inputs[1].value.trim();
    if (!cmd || !resp) return;
    const cooldown      = parseInt(inputs[2].value) || 0;
    const cooldown_type = inputs[3].value;
    const rolesRaw      = inputs[4].value.trim();
    const allowed_roles = rolesRaw
      ? rolesRaw.split(',').map(r => r.trim().toLowerCase()).filter(Boolean)
      : [];
    cmds[cmd] = { response: resp, cooldown, cooldown_type, allowed_roles };
  });
  return cmds;
}
```

- [ ] **Step 6: Update `showTab` to load new tabs**

Find `showTab` (around line 994). Add after `if (name === 'files') loadDataFiles();`:

```javascript
  if (name === 'roles') loadRoles();
  if (name === 'counters') loadCounters();
  if (name === 'quotes') loadQuotes();
```

- [ ] **Step 7: Update `saveSettings` to include `quote_addquote_role`**

In `saveSettings` body object (around line 1044), add after `ai_context_size`:

```javascript
    quote_addquote_role: el('s-quote-addquote-role').value,
```

- [ ] **Step 8: Update `saveSettings` load section to populate `quote_addquote_role`**

In the Settings modal open handler, the settings are loaded from the state. Find the `saveSettings` click handler — the modal reads values from the DOM when "Save Settings" is clicked. We need to populate the dropdown when the Settings modal opens.

Find `openSettings` or the gear button's click handler. If there's no `openSettings` function, find where the settings modal is opened and where fields are pre-filled. Look for where `s-channel`, `s-provider`, etc. are set.

In the `loadState` function's `.then(s => {...})` block (around line 664), add after the last field population:

```javascript
    el('s-quote-addquote-role').value = s.quote_addquote_role || 'moderator';
```

- [ ] **Step 9: Add JS functions for Roles, Counters, Quotes tabs**

Add these functions before the closing `</script>` tag:

```javascript
// ── Roles tab ─────────────────────────────────────────────────────────────
function loadRoles() {
  api('/api/roles').then(d => {
    const box = el('roles-list');
    box.innerHTML = '';
    const roles = d.roles || {};
    if (!Object.keys(roles).length) {
      box.innerHTML = '<div style="color:var(--muted);font-size:13px">No custom roles yet.</div>';
      return;
    }
    Object.entries(roles).forEach(([role, members]) => {
      const section = document.createElement('div');
      section.style.cssText = 'margin-bottom:12px;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:6px';
      const header = `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <strong style="font-size:13px">${escHtml(role)}</strong>
        <button class="btn btn-red btn-sm" onclick="deleteRole('${escHtml(role)}')">Delete role</button>
      </div>`;
      const memberHtml = members.map(m =>
        `<span style="display:inline-flex;align-items:center;gap:4px;background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:2px 6px;font-size:12px;margin:2px">
          ${escHtml(m)}
          <button class="btn-icon" style="font-size:10px" onclick="removeRoleMember('${escHtml(role)}','${escHtml(m)}')">✕</button>
        </span>`
      ).join('');
      const addRow = `<div style="display:flex;gap:6px;margin-top:6px">
        <input type="text" placeholder="Add username…" id="add-member-${escHtml(role)}" style="flex:1;font-size:12px">
        <button class="btn btn-neutral btn-sm" onclick="addRoleMember('${escHtml(role)}')">Add</button>
      </div>`;
      section.innerHTML = header + memberHtml + addRow;
      box.appendChild(section);
    });
  });
}

function createRole() {
  const name = el('new-role-name').value.trim().toLowerCase();
  if (!name) return;
  api('/api/roles/' + encodeURIComponent(name) + '/members', 'POST', {user: '_placeholder_'})
    .then(() => api('/api/roles/' + encodeURIComponent(name) + '/members/_placeholder_', 'DELETE'))
    .then(() => { el('new-role-name').value = ''; loadRoles(); });
}

function deleteRole(role) {
  if (!confirm('Delete role "' + role + '" and all its members?')) return;
  api('/api/roles/' + encodeURIComponent(role), 'DELETE').then(loadRoles);
}

function addRoleMember(role) {
  const inp  = el('add-member-' + role);
  const user = inp.value.trim().toLowerCase();
  if (!user) return;
  api('/api/roles/' + encodeURIComponent(role) + '/members', 'POST', {user})
    .then(() => { inp.value = ''; loadRoles(); });
}

function removeRoleMember(role, user) {
  api('/api/roles/' + encodeURIComponent(role) + '/members/' + encodeURIComponent(user), 'DELETE')
    .then(loadRoles);
}

// ── Counters tab ───────────────────────────────────────────────────────────
function loadCounters() {
  api('/api/counters').then(d => {
    const tbody = el('counters-rows');
    tbody.innerHTML = '';
    const counters = d.counters || {};
    if (!Object.keys(counters).length) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td colspan="5" style="padding:8px;color:var(--muted);font-size:13px">No counters yet.</td>`;
      tbody.appendChild(tr);
      return;
    }
    Object.entries(counters).forEach(([name, entry]) => {
      const tr = document.createElement('tr');
      const rolesVal = (entry.edit_roles || []).join(', ');
      tr.innerHTML = `
        <td style="padding:4px 6px;font-size:13px"><strong>!${escHtml(name)}</strong></td>
        <td style="padding:2px 4px;text-align:center"><input type="number" value="${entry.value||0}" style="width:60px" onchange="patchCounter('${escHtml(name)}',{value:parseInt(this.value)||0})"></td>
        <td style="padding:2px 4px"><input type="text" value="${escHtml(entry.display||'')}" style="width:100%" onchange="patchCounter('${escHtml(name)}',{display:this.value})"></td>
        <td style="padding:2px 4px"><input type="text" value="${escHtml(rolesVal)}" style="width:100%" placeholder="moderator, broadcaster" onchange="patchCounter('${escHtml(name)}',{edit_roles:this.value.split(',').map(r=>r.trim()).filter(Boolean)})"></td>
        <td style="padding:2px 4px;text-align:center"><button class="btn-icon" onclick="deleteCounter('${escHtml(name)}')">✕</button></td>
      `;
      tbody.appendChild(tr);
    });
  });
}

function addCounter() {
  const name = el('new-counter-name').value.trim().toLowerCase();
  if (!name) return;
  api('/api/counters', 'POST', {name}).then(() => { el('new-counter-name').value = ''; loadCounters(); });
}

function patchCounter(name, patch) {
  api('/api/counters/' + encodeURIComponent(name), 'PATCH', patch);
}

function deleteCounter(name) {
  if (!confirm('Delete counter !' + name + '?')) return;
  api('/api/counters/' + encodeURIComponent(name), 'DELETE').then(loadCounters);
}

// ── Quotes tab ─────────────────────────────────────────────────────────────
function loadQuotes() {
  const q = (el('quote-search') && el('quote-search').value) || '';
  api('/api/quotes' + (q ? '?q=' + encodeURIComponent(q) : '')).then(d => {
    const tbody = el('quotes-rows');
    const empty = el('quotes-empty');
    tbody.innerHTML = '';
    const quotes = d.quotes || [];
    if (!quotes.length) {
      empty.style.display = '';
      return;
    }
    empty.style.display = 'none';
    quotes.forEach(q => {
      const tr = document.createElement('tr');
      const date = (q.timestamp || '').slice(0, 10);
      tr.innerHTML = `
        <td style="padding:4px 6px;font-size:12px;color:var(--muted)">#${q.id}</td>
        <td style="padding:4px 6px;font-size:12px">${escHtml(q.text)}</td>
        <td style="padding:4px 6px;font-size:12px;color:var(--muted)">${escHtml(q.added_by||'')}</td>
        <td style="padding:4px 6px;font-size:11px;color:var(--muted)">${escHtml(date)}</td>
        <td style="padding:2px 4px;text-align:center"><button class="btn-icon" onclick="deleteQuote(${q.id})">✕</button></td>
      `;
      tbody.appendChild(tr);
    });
  });
}

function deleteQuote(id) {
  if (!confirm('Delete quote #' + id + '?')) return;
  api('/api/quotes/' + id, 'DELETE').then(loadQuotes);
}
```

- [ ] **Step 10: Run full test suite**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && python -m pytest tests/ -v 2>&1 | tail -15
```
Expected: all pass (HTML/JS changes don't affect Python tests)

- [ ] **Step 11: Commit**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && git add templates/index.html && git commit -m "feat(ui): add Roles, Counters, Quotes tabs to Settings modal; add allowed_roles column to Commands"
```

---

## Task 8: Push and update docs

- [ ] **Step 1: Run full test suite one final time**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && python -m pytest tests/ -v
```
Expected: all tests pass

- [ ] **Step 2: Push to remote**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && git push
```

- [ ] **Step 3: Update CLAUDE.md**

Add to the CLAUDE.md "Custom `!command` responses" section a note about role gating:

> **Role gating:** Each command entry has an `"allowed_roles": []` field. Empty = anyone. Non-empty = user must have at least one of the listed roles (checked against IRC badge roles + custom roles from `data/roles.json`).

And add new sections for:
- Role system (`data/roles.json`, `_build_user_roles`, `_route_role_commands`)
- Counter commands (`data/counters.json`, `_route_counters`)
- Quote system (`data/quotes.json`, `_route_quotes`)

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && git add CLAUDE.md && git commit -m "docs: update CLAUDE.md for roles, counters, quotes systems"
```

---

## Self-Review

**Spec coverage check:**
- ✅ Role system: `_build_user_roles` + `_route_role_commands` + API routes + UI tab
- ✅ Twitch native roles auto-count: badge parsing in `_build_user_roles`
- ✅ Custom roles assigned via chat + web UI
- ✅ Counter commands with presets (deaths/wins/losses)
- ✅ Counter role-gating on edit operations
- ✅ Counter API routes + UI tab
- ✅ Quote system with role-gated `!addquote`
- ✅ Quote API routes + UI tab with search
- ✅ Command gating via `allowed_roles` field
- ✅ `allowed_roles` migration (injected on settings load)
- ✅ Dispatch wiring: role commands → counters → quotes → commands → plays → AI
- ✅ `quote_addquote_role` config key in settings

**Placeholder scan:** None found.

**Type consistency:** `_build_user_roles` returns `set[str]` throughout. `_route_counters` and `_route_quotes` return `bool`. `user_roles: set[str] | None = None` default on `_route_chat_commands` preserves backward compat.
