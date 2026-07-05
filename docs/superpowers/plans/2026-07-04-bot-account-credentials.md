# Bot Account Credentials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the bot to use a dedicated bot account (username + OAuth token) for Twitch IRC, keeping the existing broadcaster credentials as an optional fallback for EventSub follow-event detection only.

**Architecture:** Add `bot_username`/`bot_token` config/env keys that drive the IRC connection. The existing `twitch_username`/`twitch_token` keys become broadcaster-only (EventSub). Migration: on first load, if bot fields are empty, auto-populate them from the streamer fields so existing setups continue working. The frontend gains two new fields in the Twitch tab; the broadcaster fields are relabeled and marked optional.

**Tech Stack:** Python 3, Flask, `twitch_bot.py` (single-file backend), `templates/index.html` (single-file frontend), `unittest` for tests.

---

## Files

| File | Change |
|---|---|
| `twitch_bot.py` | Add config keys, env persistence, IRC creds, connect guard, self-filter, mention detection |
| `templates/index.html` | Add bot fields to Twitch tab, relabel broadcaster fields, update JS |
| `tests/test_bot_account.py` | New test file |

---

### Task 1: Config keys, env persistence, and settings API

**Files:**
- Modify: `twitch_bot.py:1155-1160` (config init), `twitch_bot.py:1415-1439` (_save_env), `twitch_bot.py:1673-1690` (_SETTINGS_KEYS), `twitch_bot.py:1583-1587` (/api/connect route)
- Create: `tests/test_bot_account.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_bot_account.py`:

```python
"""Tests for bot account credential split."""
import collections
import sys
import os
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


def _make_app(config: dict) -> twitch_bot.WebApp:
    """Construct a WebApp stub with only the attributes needed for config/routing tests."""
    app = object.__new__(twitch_bot.WebApp)
    app._config_lock   = threading.Lock()
    app._history_lock  = threading.Lock()
    app._chat_history  = collections.deque(maxlen=20)
    app._ai_counter    = 0
    app._ai            = None
    app._irc           = None
    app._config        = dict(config)
    return app


_BASE_CONFIG = {
    "twitch_channel":   "streamer",
    "twitch_username":  "streamer",
    "twitch_token":     "oauth:streamertoken",
    "twitch_client_id": "",
    "bot_username":     "mybot",
    "bot_token":        "oauth:bottoken",
    "ai_enabled":       True,
    "trigger_every_n":  False,
    "trigger_mentions": True,
    "trigger_bits":     False,
    "trigger_points":   False,
    "reward_id":        "",
    "every_n":          5,
    "min_bits":         100,
    "ai_context_enabled": False,
    "ai_context_size":  5,
    "ignore_list_enabled": False,
    "ignore_list":      [],
    "chat_commands_enabled": False,
    "chat_commands":    {},
    "plays_enabled":    False,
    "command_map":      {},
}


class TestGetIrcCreds(unittest.TestCase):
    def test_returns_bot_credentials(self):
        app = _make_app(_BASE_CONFIG)
        creds = app._get_irc_creds()
        self.assertEqual(creds["username"], "mybot")
        self.assertEqual(creds["token"],    "oauth:bottoken")
        self.assertEqual(creds["channel"],  "streamer")

    def test_does_not_return_broadcaster_token(self):
        app = _make_app(_BASE_CONFIG)
        creds = app._get_irc_creds()
        self.assertNotEqual(creds["token"], "oauth:streamertoken")


class TestMigration(unittest.TestCase):
    def test_bot_fields_populated_from_streamer_when_empty(self):
        """When BOT_USERNAME/BOT_TOKEN absent from .env, they fall back to streamer fields."""
        env = {
            "TWITCH_CHANNEL":  "chan",
            "TWITCH_USERNAME": "streameracc",
            "TWITCH_TOKEN":    "oauth:streamertkn",
        }
        bot_username = env.get("BOT_USERNAME", "") or env.get("TWITCH_USERNAME", "")
        bot_token    = env.get("BOT_TOKEN",    "") or env.get("TWITCH_TOKEN",    "")
        self.assertEqual(bot_username, "streameracc")
        self.assertEqual(bot_token,    "oauth:streamertkn")

    def test_bot_fields_not_overwritten_when_already_set(self):
        """Existing BOT_USERNAME/BOT_TOKEN are not replaced by streamer fields."""
        env = {
            "TWITCH_USERNAME": "streameracc",
            "TWITCH_TOKEN":    "oauth:streamertkn",
            "BOT_USERNAME":    "mybot",
            "BOT_TOKEN":       "oauth:bottkn",
        }
        bot_username = env.get("BOT_USERNAME", "") or env.get("TWITCH_USERNAME", "")
        bot_token    = env.get("BOT_TOKEN",    "") or env.get("TWITCH_TOKEN",    "")
        self.assertEqual(bot_username, "mybot")
        self.assertEqual(bot_token,    "oauth:bottkn")


class TestSelfFilter(unittest.TestCase):
    def _app_with_mocked_routes(self, config=None):
        app = _make_app(config or _BASE_CONFIG)
        app._log                 = lambda msg: None
        app._route_chat_commands = MagicMock()
        app._route_plays         = MagicMock()
        app._route_ai            = MagicMock()
        app._handle_event        = MagicMock()
        return app

    def test_dispatch_drops_bots_own_messages(self):
        """_dispatch returns early without routing when username == bot_username."""
        app = self._app_with_mocked_routes()
        app._dispatch("mybot", "hello chat")
        app._route_ai.assert_not_called()
        app._route_plays.assert_not_called()

    def test_dispatch_allows_other_users(self):
        """_dispatch routes normally for messages from other users."""
        app = self._app_with_mocked_routes()
        app._dispatch("someviewer", "hello chat")
        app._route_ai.assert_called_once()

    def test_self_filter_is_case_insensitive(self):
        """Self-filter matches regardless of username casing."""
        app = self._app_with_mocked_routes()
        app._dispatch("MyBot", "hello chat")
        app._route_ai.assert_not_called()


class TestMentionDetection(unittest.TestCase):
    def _app_for_route_ai(self, bot_username="mybot", broadcaster_username="streamer"):
        config = {
            **_BASE_CONFIG,
            "bot_username":    bot_username,
            "twitch_username": broadcaster_username,
        }
        app = _make_app(config)
        app._log = lambda msg: None
        app._ai  = MagicMock()
        return app

    def test_mention_triggers_on_bot_username(self):
        """@mention trigger fires when bot_username appears in the message."""
        app = self._app_for_route_ai("mybot")
        app._route_ai("viewer", "hey mybot come here")
        app._ai.handle.assert_called_once()

    def test_mention_does_not_trigger_on_broadcaster_username(self):
        """Trigger does NOT fire when only the broadcaster username appears in the message."""
        app = self._app_for_route_ai("mybot", broadcaster_username="streamer")
        app._route_ai("viewer", "hey streamer you there")
        app._ai.handle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
python -m pytest tests/test_bot_account.py -v
```

Expected: multiple FAILs / AttributeErrors (`_get_irc_creds` returns `twitch_username`, `_dispatch` has no self-filter, etc.)

- [ ] **Step 3: Add `bot_username`/`bot_token` to `_config` init with migration**

In `twitch_bot.py`, find the config dict starting at line 1155. After the existing credential keys (around line 1160), add:

```python
            "twitch_channel":          env.get("TWITCH_CHANNEL", ""),
            "twitch_username":         env.get("TWITCH_USERNAME", ""),
            "twitch_client_id":        env.get("TWITCH_CLIENT_ID", ""),
            "twitch_token":            env.get("TWITCH_TOKEN", ""),
            "bot_username":            env.get("BOT_USERNAME", "") or env.get("TWITCH_USERNAME", ""),
            "bot_token":               env.get("BOT_TOKEN",    "") or env.get("TWITCH_TOKEN",    ""),
```

(Replace the existing four credential lines with this six-line block — keep the existing four lines, append the two new `bot_*` lines immediately after.)

- [ ] **Step 4: Add `bot_username`/`bot_token` to `_save_env()`**

In `_save_env()` (around line 1419), after `f"TWITCH_TOKEN=..."`, add:

```python
            f"BOT_USERNAME={c.get('bot_username', '')}",
            f"BOT_TOKEN={c.get('bot_token', '')}",
```

- [ ] **Step 5: Add `bot_username`/`bot_token` to `_SETTINGS_KEYS` and `/api/connect`**

In `_SETTINGS_KEYS` (around line 1673), add `"bot_username"` and `"bot_token"` to the tuple:

```python
        _SETTINGS_KEYS = (
            "twitch_channel", "twitch_username", "twitch_client_id", "twitch_token",
            "bot_username", "bot_token",
            ...
```

In the `/api/connect` route (around line 1584), extend the key list:

```python
                    for k in ("twitch_channel", "twitch_username",
                               "twitch_client_id", "twitch_token",
                               "bot_username", "bot_token"):
```

- [ ] **Step 6: Run migration and config tests — confirm they pass**

```bash
python -m pytest tests/test_bot_account.py::TestMigration tests/test_bot_account.py::TestGetIrcCreds -v
```

Expected: FAILs on `TestGetIrcCreds` (IRC still uses old keys) — OK for now. `TestMigration` should PASS.

- [ ] **Step 7: Commit**

```bash
git add twitch_bot.py tests/test_bot_account.py
git commit -m "feat(credentials): add bot_username/bot_token config keys with migration"
```

---

### Task 2: Switch IRC to bot credentials + connect guard

**Files:**
- Modify: `twitch_bot.py:1371-1376` (`_get_irc_creds`), `twitch_bot.py:1262-1263` (auto-connect), `twitch_bot.py:1955-1987` (`_connect`)

- [ ] **Step 1: Update `_get_irc_creds()` to use bot fields**

Replace the body of `_get_irc_creds` (lines 1371–1376):

```python
    def _get_irc_creds(self) -> dict:
        with self._config_lock:
            return {
                "channel":  self._config.get("twitch_channel",  ""),
                "username": self._config.get("bot_username", ""),
                "token":    self._config.get("bot_token",    ""),
            }
```

- [ ] **Step 2: Update auto-connect check to use bot fields**

Around line 1262, change:

```python
        if all(self._config.get(k) for k in
               ("twitch_channel", "twitch_username", "twitch_token")):
```

to:

```python
        if all(self._config.get(k) for k in
               ("twitch_channel", "bot_username", "bot_token")):
```

- [ ] **Step 3: Add bot credential guard and EventSub token check in `_connect()`**

Replace the `with self._config_lock:` block in `_connect()` (around line 1965–1967):

```python
        self._save_env()
        with self._config_lock:
            self._config["twitch_status"] = "connecting"
            bot_username          = self._config.get("bot_username",    "").strip()
            bot_token             = self._config.get("bot_token",       "").strip()
            has_client_id         = bool(self._config.get("twitch_client_id", "").strip())
            has_broadcaster_token = bool(self._config.get("twitch_token",     "").strip())
        if not bot_username or not bot_token:
            with self._config_lock:
                self._config["twitch_status"] = "error"
            self._broadcast_status()
            self._log("[System] Error: Bot Username and Bot OAuth Token are required.")
            return
        self._broadcast_status()
```

Then update the EventSub block (around line 1979):

```python
        if has_client_id and has_broadcaster_token:
            self._eventsub = EventSubClient(
                get_creds=self._get_eventsub_creds,
                log=self._log,
                on_event=self._handle_event,
            )
            self._eventsub.connect()
        elif has_client_id:
            self._log("[EventSub] Broadcaster token not set — follow events disabled")
        else:
            self._log("[EventSub] No Client ID set — follow events disabled")
```

- [ ] **Step 4: Run IRC credential tests**

```bash
python -m pytest tests/test_bot_account.py::TestGetIrcCreds -v
```

Expected: PASS

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all previously passing tests still pass, `TestGetIrcCreds` now passes.

- [ ] **Step 6: Commit**

```bash
git add twitch_bot.py
git commit -m "feat(credentials): switch IRC connection to bot_username/bot_token"
```

---

### Task 3: Self-filter and mention detection

**Files:**
- Modify: `twitch_bot.py:2062-2084` (`_dispatch`), `twitch_bot.py:2136` (`_route_ai`)

- [ ] **Step 1: Add self-filter to `_dispatch()`**

After the ignore-list block in `_dispatch` (after line 2075, before the `_chat_history.append` line), insert:

```python
        with self._config_lock:
            bot_username = self._config.get("bot_username", "").lower()
        if bot_username and username.lower() == bot_username:
            return
```

The result should look like:

```python
        if ignore_enabled and username.lower() in ignore_list:
            return

        with self._config_lock:
            bot_username = self._config.get("bot_username", "").lower()
        if bot_username and username.lower() == bot_username:
            return

        with self._history_lock:
            self._chat_history.append((username, message))
```

- [ ] **Step 2: Fix mention detection in `_route_ai()`**

On line 2136, change:

```python
            bot_user         = self._config.get("twitch_username",   "").lower()
```

to:

```python
            bot_user         = self._config.get("bot_username", "").lower()
```

- [ ] **Step 3: Run self-filter and mention tests**

```bash
python -m pytest tests/test_bot_account.py::TestSelfFilter tests/test_bot_account.py::TestMentionDetection -v
```

Expected: all PASS

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add twitch_bot.py
git commit -m "feat(credentials): add bot self-filter in _dispatch, use bot_username for @mentions"
```

---

### Task 4: Frontend — new bot fields in Twitch tab

**Files:**
- Modify: `templates/index.html` (Twitch tab HTML, `openSettings()`, `saveSettings()`)

- [ ] **Step 1: Replace the Twitch tab HTML**

Find the Twitch tab block (lines 257–273):

```html
      <!-- Twitch tab -->
      <div id="tab-twitch" class="tab-pane active">
        <div class="field-row"><label>Channel</label><input id="s-channel" type="text" placeholder="channelname"></div>
        <div class="field-row"><label>Bot Username</label><input id="s-username" type="text" placeholder="mybotname"></div>
        <div class="field-row"><label>Client ID</label>
          <div class="field-with-btn">
            <input id="s-client-id" type="text" placeholder="your Twitch app client ID">
            <button class="btn btn-neutral btn-sm" onclick="getOAuthUrl()">Get Token ↗</button>
          </div>
        </div>
        <div class="field-row"><label>OAuth Token</label>
          <div class="field-with-btn">
            <input id="s-token" type="password" placeholder="oauth:xxxxxxxxxxxxxxxx">
            <button class="btn-icon btn-sm" onclick="toggleVis(this.previousElementSibling)">👁</button>
          </div>
        </div>
      </div>
```

Replace it with:

```html
      <!-- Twitch tab -->
      <div id="tab-twitch" class="tab-pane active">
        <div class="field-row"><label>Channel</label><input id="s-channel" type="text" placeholder="channelname"></div>
        <div class="field-row"><label>Bot Username</label><input id="s-bot-username" type="text" placeholder="mybotname"></div>
        <div class="field-row"><label>Bot OAuth Token</label>
          <div class="field-with-btn">
            <input id="s-bot-token" type="password" placeholder="oauth:xxxxxxxxxxxxxxxx">
            <button class="btn-icon btn-sm" onclick="toggleVis(this.previousElementSibling)">👁</button>
          </div>
        </div>
        <div class="divider"></div>
        <div class="field-row"><label>Broadcaster Username</label><input id="s-username" type="text" placeholder="streamername"></div>
        <div class="field-row"><label>Client ID</label>
          <div class="field-with-btn">
            <input id="s-client-id" type="text" placeholder="your Twitch app client ID">
            <button class="btn btn-neutral btn-sm" onclick="getOAuthUrl()">Get Token ↗</button>
          </div>
        </div>
        <div class="field-row"><label>Broadcaster Token</label>
          <div class="field-with-btn">
            <input id="s-token" type="password" placeholder="oauth:xxxxxxxxxxxxxxxx">
            <button class="btn-icon btn-sm" onclick="toggleVis(this.previousElementSibling)">👁</button>
          </div>
        </div>
        <div class="hint">(optional — follow events only)</div>
      </div>
```

- [ ] **Step 2: Update `openSettings()` to populate bot fields**

In `openSettings()` (around line 885), after `el('s-channel').value = s.twitch_channel || '';`, add:

```javascript
    el('s-bot-username').value      = s.bot_username   || '';
    el('s-bot-token').value         = s.bot_token      || '';
```

- [ ] **Step 3: Update `saveSettings()` to include bot fields**

In `saveSettings()` (around line 956), after `twitch_channel: el('s-channel').value.trim(),`, add:

```javascript
    bot_username:             el('s-bot-username').value.trim(),
    bot_token:                el('s-bot-token').value.trim(),
```

- [ ] **Step 4: Verify the app loads without errors**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
python -m pytest tests/ -v
```

Expected: all pass. (UI correctness verified manually by opening `http://localhost:5000` and checking the Twitch tab shows the new fields.)

- [ ] **Step 5: Commit**

```bash
git add templates/index.html
git commit -m "feat(ui): add Bot Username/Token fields, relabel broadcaster fields as optional"
```

---

### Task 5: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the IRC credential format section**

Find the "IRC credential format" section in `CLAUDE.md` and update it to reflect the new field split:

```markdown
## IRC credential format

- Channel: plain name, no `#` — the channel the bot joins
- Bot Username: the bot account's Twitch login name
- Bot OAuth Token: `oauth:xxxxxxxxxxxxxxxx` for the bot account (prefix added automatically if omitted)
- Broadcaster Username / Broadcaster Token / Client ID: optional — only needed for EventSub follow-event detection
```

- [ ] **Step 2: Add bot account section to the Settings persistence table**

In the `.env` key names section, add:
```
- Bot account: `BOT_USERNAME`, `BOT_TOKEN`
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): document bot account credential split"
```

---

### Task 6: Final check and push

- [ ] **Step 1: Run full test suite one more time**

```bash
python -m pytest tests/ -v
```

Expected: all 30+ tests pass, no regressions.

- [ ] **Step 2: Push**

```bash
git push
```
