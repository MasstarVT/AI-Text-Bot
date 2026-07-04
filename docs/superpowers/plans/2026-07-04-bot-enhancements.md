# Bot Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add five quality-of-life features to the Twitch bot: ignore list, thanks cooldown, custom `!commands`, scheduled messages, and AI chat context window.

**Architecture:** All five features follow the same pattern: (1) new config keys in `_SETTINGS_DEFAULTS` + `__init__` + `_save_settings`, (2) new logic in the relevant handler method, (3) new API surface in `_SETTINGS_KEYS` / `_BOOL_KEYS` / `_INT_KEYS`, and (4) corresponding HTML/JS in the settings modal. Features are independent — implement them sequentially to avoid merge conflicts.

**Tech Stack:** Python 3 (`threading`, `collections.deque`, `time`), Flask, vanilla JS in `templates/index.html`.

---

## Codebase orientation

Single file: `twitch_bot.py` (~1850 lines). One HTML template: `templates/index.html`.

Key methods and their approximate line numbers (verify with `grep -n` before editing):
- `WebApp._SETTINGS_DEFAULTS` — dict literal, ~line 903
- `WebApp.__init__` → `self._config` block — ~line 948
- `WebApp._save_settings` — ~line 1188
- `WebApp._register_routes` → `_SETTINGS_KEYS`, `_BOOL_KEYS`, `api_settings_post` — ~line 1396
- `WebApp._dispatch` — ~line 1732
- `WebApp._route_ai` — ~line 1763
- `WebApp._handle_event` — ~line 1805
- `AIResponseHandler.handle` — ~line 386
- `AIResponseHandler._worker` — ~line 508
- `AIResponseHandler._query` — ~line 523
- `AIResponseHandler._stream_openai` — ~line 398
- `AIResponseHandler._stream_anthropic` — ~line 451

Settings modal tab bar is at ~line 245 in `templates/index.html`. The five existing tabs are: Twitch, AI, Discord, TTS, Thanks.

Thread-safety rules (from CLAUDE.md):
- Always snapshot shared object references before truthiness check: `irc = self._irc; if irc:`
- Config reads from worker threads go through `get_config()` callables, not direct dict access.
- For ad-hoc reads in `WebApp` methods (which run on the IRC thread or request threads), acquire `self._config_lock` and snapshot, then release before doing work.

---

## Task 1: Ignore list

Add a per-username ignore list. When a username is in the list, `_dispatch` returns early — no AI, no plays, no thanks.

**Files:**
- Modify: `twitch_bot.py` (5 locations: defaults, config init, save_settings, API keys, _dispatch)
- Modify: `templates/index.html` (tab button, tab pane, openSettings, saveSettings)

- [ ] **Step 1: Add config defaults**

In `_SETTINGS_DEFAULTS` (after `"thanks_prompt": ""`):

```python
        # ── ignore list ────────────────────────────────────────────────────────
        "ignore_list_enabled": False,
        "ignore_list":         [],
```

- [ ] **Step 2: Load config in `__init__`**

In the `self._config` block (after the `"thanks_prompt"` line):

```python
            "ignore_list_enabled": settings.get("ignore_list_enabled", False),
            "ignore_list":         [str(u).lower().strip() for u in settings.get("ignore_list", []) if u],
```

- [ ] **Step 3: Persist in `_save_settings`**

In `_save_settings` data dict (after `"thanks_prompt"` line):

```python
            # ── ignore list ────────────────────────────────────────────────────
            "ignore_list_enabled": c.get("ignore_list_enabled", False),
            "ignore_list":         c.get("ignore_list",         []),
```

- [ ] **Step 4: Expose via API**

In `_SETTINGS_KEYS` tuple, add after `"thanks_prompt"`:

```python
            "ignore_list_enabled", "ignore_list",
```

In `_BOOL_KEYS` set, add:

```python
                "ignore_list_enabled",
```

In `api_settings_post`, inside the `for k in _SETTINGS_KEYS` loop, the existing `elif k in _BOOL_KEYS` branch handles `ignore_list_enabled`. Add a special branch for `ignore_list` (a list) **before** the `elif k in _BOOL_KEYS` line:

```python
                        if k == "ignore_list":
                            if isinstance(data.get(k), list):
                                self._config[k] = [str(u).lower().strip() for u in data[k] if u]
                        elif k in _INT_KEYS:
```

The existing structure around the insertion point looks like:
```python
                for k in _SETTINGS_KEYS:
                    if k in data:
                        if k in _INT_KEYS:
                            ...
                        elif k in _BOOL_KEYS:
                            self._config[k] = bool(data[k])
                        else:
                            self._config[k] = data[k]
```

Change it to:
```python
                for k in _SETTINGS_KEYS:
                    if k in data:
                        if k == "ignore_list":
                            if isinstance(data[k], list):
                                self._config[k] = [str(u).lower().strip() for u in data[k] if u]
                        elif k in _INT_KEYS:
                            try:
                                self._config[k] = int(data[k])
                            except (TypeError, ValueError):
                                pass
                        elif k in _BOOL_KEYS:
                            self._config[k] = bool(data[k])
                        else:
                            self._config[k] = data[k]
```

- [ ] **Step 5: Apply the ignore check in `_dispatch`**

At the very top of `_dispatch`, after `self._log(...)` and before `self._route_plays(...)`:

```python
    def _dispatch(self, username: str, message: str,
                  bits: int = 0, reward_id: str = "") -> None:
        tag = (f"  [{bits} bits]" if bits
               else (f"  [channel points]" if reward_id else ""))
        self._log(f"[Chat] {username}{tag}: {message}")
        if reward_id:
            self._log(f"[Chat] Reward ID: {reward_id}")

        # ── ignore list ────────────────────────────────────────────────────────
        with self._config_lock:
            ignore_enabled = self._config.get("ignore_list_enabled", False)
            ignore_list    = self._config.get("ignore_list", [])
        if ignore_enabled and username.lower() in ignore_list:
            return

        self._route_plays(username, message)
        self._route_ai(username, message, bits, reward_id)
        if bits > 0:
            self._handle_event("bits", username, {"bits": bits})
```

- [ ] **Step 6: Add UI — tab button and pane**

In `templates/index.html`, add the new tab button in the `.tab-bar`:

```html
      <button class="tab"        onclick="showTab('ignore')">Ignore</button>
```

Place it after the Thanks tab button.

Add the tab pane after the `#tab-thanks` closing `</div>`:

```html
      <!-- Ignore list tab -->
      <div id="tab-ignore" class="tab-pane">
        <div class="section-lbl">Ignore List</div>
        <label class="row-check">
          <input type="checkbox" id="s-ignore-enabled">
          Enable ignore list
        </label>
        <div class="divider"></div>
        <div class="section-lbl">Ignored Usernames</div>
        <textarea id="s-ignore-list" rows="6" placeholder="one username per line&#10;(case-insensitive)"></textarea>
        <div class="hint">Messages from these users will be completely ignored — no AI, no plays, no thanks.</div>
      </div>
```

- [ ] **Step 7: Wire up openSettings and saveSettings**

In `openSettings()`, after the `toggleThanksPrompt()` call:

```javascript
    el('s-ignore-enabled').checked = !!s.ignore_list_enabled;
    el('s-ignore-list').value      = (s.ignore_list || []).join('\n');
```

In `saveSettings()`, after the `thanks_prompt` line:

```javascript
    ignore_list_enabled: el('s-ignore-enabled').checked,
    ignore_list:         el('s-ignore-list').value.split('\n').map(u => u.trim().toLowerCase()).filter(Boolean),
```

- [ ] **Step 8: Verify**

Start the bot: `.venv/bin/python twitch_bot.py`

Open settings → Ignore tab. Check "Enable ignore list", type `testuser` in the textarea. Save. Check `GET http://localhost:5000/api/settings` — should return `{"ignore_list_enabled": true, "ignore_list": ["testuser"], ...}`.

- [ ] **Step 9: Commit**

```bash
git add twitch_bot.py templates/index.html
git commit -m "feat(dispatch): add username ignore list with per-user skip of all routing"
```

---

## Task 2: Thanks response cooldown

Prevent the thanks system from firing more than once within a configurable window. Defaults to 30 seconds, disabled by default.

**Files:**
- Modify: `twitch_bot.py` (defaults, config init, save, API, _handle_event, plus a new instance attribute)
- Modify: `templates/index.html` (Thanks tab: add cooldown section, openSettings, saveSettings)

- [ ] **Step 1: Add config defaults**

In `_SETTINGS_DEFAULTS`, after `"thanks_prompt"` and before `"ignore_list_enabled"`:

```python
        "thanks_cooldown_enabled": False,
        "thanks_cooldown_secs":    30,
```

- [ ] **Step 2: Add `_last_thanks_time` instance attribute**

In `WebApp.__init__`, after `self._ai_counter = 0`:

```python
        self._last_thanks_time: float = 0.0
        self._thanks_lock = threading.Lock()
```

- [ ] **Step 3: Load config in `__init__`**

In the `self._config` block, after the `"thanks_prompt"` / `"thanks_use_shared_prompt"` lines:

```python
            "thanks_cooldown_enabled": settings.get("thanks_cooldown_enabled", False),
            "thanks_cooldown_secs":    int(settings.get("thanks_cooldown_secs", 30)),
```

- [ ] **Step 4: Persist in `_save_settings`**

```python
            "thanks_cooldown_enabled": c.get("thanks_cooldown_enabled", False),
            "thanks_cooldown_secs":    c.get("thanks_cooldown_secs",    30),
```

- [ ] **Step 5: Expose via API**

In `_SETTINGS_KEYS`, after `"thanks_use_shared_prompt", "thanks_prompt"`:

```python
            "thanks_cooldown_enabled", "thanks_cooldown_secs",
```

In `_BOOL_KEYS`:
```python
                "thanks_cooldown_enabled",
```

In `_INT_KEYS`:
```python
        _INT_KEYS = {"every_n", "min_bits", "thanks_cooldown_secs"}
```

- [ ] **Step 6: Apply the cooldown check in `_handle_event`**

After the config lock block (after reading `channel`), add the cooldown check:

```python
        # ── cooldown check ─────────────────────────────────────────────────────
        with self._config_lock:
            cooldown_enabled = self._config.get("thanks_cooldown_enabled", False)
            cooldown_secs    = self._config.get("thanks_cooldown_secs",    30)

        if cooldown_enabled:
            now = time.time()
            with self._thanks_lock:
                if now - self._last_thanks_time < cooldown_secs:
                    self._log(f"[Thanks] Cooldown active — skipping {event_type} from {username}")
                    return
                self._last_thanks_time = now
```

Place this block **after** the `if not event_map.get(event_type, False): return` check and **before** `ai = self._ai`. The full updated method body:

```python
    def _handle_event(self, event_type: str, username: str, extra: dict) -> None:
        with self._config_lock:
            if not self._config.get("thanks_enabled", False):
                return
            event_map = {
                "sub":         self._config.get("thanks_sub",     True),
                "resub":       self._config.get("thanks_resub",   True),
                "subgift":     self._config.get("thanks_gift",    True),
                "mysterygift": self._config.get("thanks_mystery", True),
                "bits":        self._config.get("thanks_bits",    False),
                "raid":        self._config.get("thanks_raid",    True),
            }
            chat_on      = self._config.get("thanks_chat",              True)
            tts_on       = self._config.get("thanks_tts",               True)
            use_shared   = self._config.get("thanks_use_shared_prompt", False)
            prompt       = None if use_shared else (self._config.get("thanks_prompt", "") or _DEFAULT_THANKS_PROMPT)
            channel      = self._config.get("twitch_channel", "").lower().strip()
            cooldown_enabled = self._config.get("thanks_cooldown_enabled", False)
            cooldown_secs    = self._config.get("thanks_cooldown_secs",    30)

        if not event_map.get(event_type, False):
            return

        if cooldown_enabled:
            now = time.time()
            with self._thanks_lock:
                if now - self._last_thanks_time < cooldown_secs:
                    self._log(f"[Thanks] Cooldown active — skipping {event_type} from {username}")
                    return
                self._last_thanks_time = now

        ai = self._ai
        if not ai:
            return

        msg = _THANKS_TEMPLATES[event_type](username, extra)
        self._log(f"[Thanks] {event_type} from {username}")

        def reply_cb(reply: str) -> None:
            self._log(f"[Thanks] → {reply}")
            if chat_on:
                irc = self._irc
                if irc and channel:
                    irc.say(channel, reply)

        ai.handle(username, msg, reply_cb=reply_cb, prompt_override=prompt, use_tts=tts_on)
```

Note: reading `cooldown_enabled` and `cooldown_secs` inside the existing config lock block (at the top) avoids a second lock acquisition.

- [ ] **Step 7: Add UI to Thanks tab**

After the Delivery section in `#tab-thanks`, before the closing `</div>`, add:

```html
        <div class="divider"></div>
        <div class="section-lbl">Cooldown</div>
        <label class="row-check">
          <input type="checkbox" id="s-thanks-cooldown-enabled" onchange="toggleThanksCooldown()">
          Enable response cooldown
        </label>
        <div id="s-thanks-cooldown-wrap" style="display:none">
          <div class="field-row" style="margin-top:6px">
            <label>Seconds between responses</label>
            <input type="number" id="s-thanks-cooldown-secs" min="1" max="3600" style="width:80px">
          </div>
          <div class="hint">After a thank-you fires, ignore events for this many seconds.</div>
        </div>
```

Add JS toggle function after `toggleThanksPrompt()`:

```javascript
function toggleThanksCooldown() {
  el('s-thanks-cooldown-wrap').style.display =
    el('s-thanks-cooldown-enabled').checked ? '' : 'none';
}
```

In `openSettings()`:

```javascript
    el('s-thanks-cooldown-enabled').checked = !!s.thanks_cooldown_enabled;
    el('s-thanks-cooldown-secs').value      = s.thanks_cooldown_secs ?? 30;
    toggleThanksCooldown();
```

In `saveSettings()`:

```javascript
    thanks_cooldown_enabled: el('s-thanks-cooldown-enabled').checked,
    thanks_cooldown_secs:    parseInt(el('s-thanks-cooldown-secs').value) || 30,
```

- [ ] **Step 8: Verify**

Enable cooldown with 10 seconds. In the console, manually check that after a thanks event fires the next one within 10 seconds is logged as skipped.

- [ ] **Step 9: Commit**

```bash
git add twitch_bot.py templates/index.html
git commit -m "feat(thanks): add per-event cooldown to prevent thank-you spam"
```

---

## Task 3: Custom `!command` responses

When a message starts with `!word` and that word is in the chat commands map, post the configured response to chat directly — no AI involved.

**Files:**
- Modify: `twitch_bot.py`
- Modify: `templates/index.html`

- [ ] **Step 1: Add config defaults**

In `_SETTINGS_DEFAULTS`:

```python
        # ── chat commands ──────────────────────────────────────────────────────
        "chat_commands_enabled": False,
        "chat_commands":         {},
```

- [ ] **Step 2: Load config in `__init__`**

```python
            "chat_commands_enabled": settings.get("chat_commands_enabled", False),
            "chat_commands":         dict(settings.get("chat_commands", {})),
```

- [ ] **Step 3: Persist in `_save_settings`**

```python
            "chat_commands_enabled": c.get("chat_commands_enabled", False),
            "chat_commands":         c.get("chat_commands",         {}),
```

- [ ] **Step 4: Expose via API**

In `_SETTINGS_KEYS`:
```python
            "chat_commands_enabled", "chat_commands",
```

In `_BOOL_KEYS`:
```python
                "chat_commands_enabled",
```

In `api_settings_post`, add a branch for `chat_commands` (a dict), similar to the `ignore_list` branch added in Task 1. Inside the `for k in _SETTINGS_KEYS` loop:

```python
                        if k == "ignore_list":
                            if isinstance(data[k], list):
                                self._config[k] = [str(u).lower().strip() for u in data[k] if u]
                        elif k == "chat_commands":
                            if isinstance(data[k], dict):
                                self._config[k] = {
                                    str(cmd).lower().strip(): str(resp)
                                    for cmd, resp in data[k].items()
                                    if cmd and resp
                                }
                        elif k in _INT_KEYS:
                            ...
```

- [ ] **Step 5: Add `_route_chat_commands` method**

Add this method to `WebApp`, just before `_route_plays`:

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
        irc = self._irc
        if irc:
            irc.say(channel, response)
            self._log(f"[Commands] {username} → {word}")
```

- [ ] **Step 6: Call it from `_dispatch`**

After the ignore list check in `_dispatch`, add:

```python
        self._route_chat_commands(username, message)
        self._route_plays(username, message)
        self._route_ai(username, message, bits, reward_id)
        if bits > 0:
            self._handle_event("bits", username, {"bits": bits})
```

- [ ] **Step 7: Add UI — new "Commands" tab**

Add tab button in `.tab-bar` (after the Ignore tab button):

```html
      <button class="tab"        onclick="showTab('commands')">Commands</button>
```

Add tab pane (after `#tab-ignore`):

```html
      <!-- Commands tab -->
      <div id="tab-commands" class="tab-pane">
        <div class="section-lbl">Chat Commands</div>
        <label class="row-check">
          <input type="checkbox" id="s-cmd-enabled">
          Enable custom !commands
        </label>
        <div class="hint">When a viewer types a !command, the bot posts the configured response without using AI.</div>
        <div class="divider"></div>
        <table id="cmd-table" style="width:100%;border-collapse:collapse;margin-bottom:8px">
          <thead>
            <tr>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Command</th>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Response</th>
              <th style="width:32px"></th>
            </tr>
          </thead>
          <tbody id="cmd-rows"></tbody>
        </table>
        <button class="btn btn-neutral btn-sm" onclick="addCmdRow()">+ Add command</button>
      </div>
```

- [ ] **Step 8: Add JS helper functions**

Add these functions near `toggleDiscordPrompt()`:

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

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

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

- [ ] **Step 9: Wire openSettings and saveSettings**

In `openSettings()`:
```javascript
    el('s-cmd-enabled').checked = !!s.chat_commands_enabled;
    el('cmd-rows').innerHTML = '';
    Object.entries(s.chat_commands || {}).forEach(([cmd, resp]) => addCmdRow(cmd, resp));
```

In `saveSettings()`:
```javascript
    chat_commands_enabled: el('s-cmd-enabled').checked,
    chat_commands:         getChatCommands(),
```

- [ ] **Step 10: Verify**

Add a command `!discord` → `"Join our Discord at discord.gg/example"`. Save. Send `!discord` in chat — the bot should reply. Check the console for `[Commands] <username> → !discord`.

- [ ] **Step 11: Commit**

```bash
git add twitch_bot.py templates/index.html
git commit -m "feat(dispatch): add custom !command responses without AI"
```

---

## Task 4: Scheduled messages

Post a message to Twitch chat on a repeating interval. Multiple entries, each with its own interval in minutes. Uses a single long-running daemon thread that checks every 30 seconds.

**Files:**
- Modify: `twitch_bot.py`
- Modify: `templates/index.html`

- [ ] **Step 1: Add config defaults**

```python
        # ── scheduled messages ─────────────────────────────────────────────────
        "scheduled_msgs": [],
```

- [ ] **Step 2: Load config in `__init__`**

```python
            "scheduled_msgs": list(settings.get("scheduled_msgs", [])),
```

- [ ] **Step 3: Persist in `_save_settings`**

```python
            "scheduled_msgs": c.get("scheduled_msgs", []),
```

- [ ] **Step 4: Expose via API**

In `_SETTINGS_KEYS`:
```python
            "scheduled_msgs",
```

In `api_settings_post`, add a branch for `scheduled_msgs` (a list of dicts):

```python
                        elif k == "scheduled_msgs":
                            if isinstance(data[k], list):
                                self._config[k] = [
                                    {"text": str(e.get("text","")).strip(),
                                     "interval": max(1, int(e.get("interval", 30)))}
                                    for e in data[k]
                                    if isinstance(e, dict) and str(e.get("text","")).strip()
                                ]
```

Place this branch alongside the `ignore_list` and `chat_commands` branches.

- [ ] **Step 5: Add the scheduler thread**

Add a method `_scheduler_loop` to `WebApp`:

```python
    def _scheduler_loop(self) -> None:
        last_fired: dict[str, float] = {}  # keyed by message text
        while True:
            time.sleep(30)
            with self._config_lock:
                msgs    = list(self._config.get("scheduled_msgs", []))
                online  = self._config.get("twitch_status") == "online"
                channel = self._config.get("twitch_channel", "").lower().strip()
            if not online or not channel or not msgs:
                continue
            now = time.time()
            for entry in msgs:
                text     = entry.get("text", "").strip()
                interval = max(1, int(entry.get("interval", 30))) * 60
                if not text:
                    continue
                if now - last_fired.get(text, 0) >= interval:
                    last_fired[text] = now
                    irc = self._irc
                    if irc:
                        irc.say(channel, text)
                        self._log(f"[Scheduled] → {text}")
```

Start it as a daemon thread in `__init__`, after `self._autosave()`:

```python
        _sched = threading.Thread(target=self._scheduler_loop, name="Scheduler", daemon=True)
        _sched.start()
```

- [ ] **Step 6: Add UI — new "Schedule" tab**

Tab button (after Commands):

```html
      <button class="tab"        onclick="showTab('schedule')">Schedule</button>
```

Tab pane (after `#tab-commands`):

```html
      <!-- Schedule tab -->
      <div id="tab-schedule" class="tab-pane">
        <div class="section-lbl">Scheduled Messages</div>
        <div class="hint">Messages posted to chat automatically on a repeating interval while connected. Checked every 30 seconds.</div>
        <div class="divider"></div>
        <table id="sched-table" style="width:100%;border-collapse:collapse;margin-bottom:8px">
          <thead>
            <tr>
              <th style="text-align:left;padding:4px 6px;font-size:11px;color:var(--muted)">Message</th>
              <th style="padding:4px 6px;font-size:11px;color:var(--muted);white-space:nowrap">Interval (min)</th>
              <th style="width:32px"></th>
            </tr>
          </thead>
          <tbody id="sched-rows"></tbody>
        </table>
        <button class="btn btn-neutral btn-sm" onclick="addSchedRow()">+ Add message</button>
      </div>
```

- [ ] **Step 7: Add JS helpers**

```javascript
function addSchedRow(text, interval) {
  const tbody = el('sched-rows');
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td style="padding:2px 4px"><input type="text" placeholder="!socials — follow me on Twitter!" style="width:100%" value="${escHtml(text||'')}"></td>
    <td style="padding:2px 4px;text-align:center"><input type="number" min="1" max="9999" style="width:70px" value="${interval||30}"></td>
    <td style="padding:2px 4px;text-align:center"><button class="btn-icon" onclick="this.closest('tr').remove()">✕</button></td>
  `;
  tbody.appendChild(tr);
}

function getSchedMsgs() {
  return Array.from(el('sched-rows').querySelectorAll('tr')).map(tr => {
    const inputs = tr.querySelectorAll('input');
    return { text: inputs[0].value.trim(), interval: parseInt(inputs[1].value) || 30 };
  }).filter(e => e.text);
}
```

- [ ] **Step 8: Wire openSettings and saveSettings**

In `openSettings()`:
```javascript
    el('sched-rows').innerHTML = '';
    (s.scheduled_msgs || []).forEach(e => addSchedRow(e.text, e.interval));
```

In `saveSettings()`:
```javascript
    scheduled_msgs: getSchedMsgs(),
```

- [ ] **Step 9: Verify**

Add a scheduled message "Test message" with interval 1 minute. Save. Wait 1 minute while connected. The console should show `[Scheduled] → Test message` and the message should appear in Twitch chat.

Faster test: temporarily set interval to 1 minute and verify the `last_fired` key prevents immediate duplicate firing (the 30s sleep means first fire happens at most 30s after the interval elapses).

- [ ] **Step 10: Commit**

```bash
git add twitch_bot.py templates/index.html
git commit -m "feat(scheduler): add repeating scheduled messages with per-message interval"
```

---

## Task 5: Chat context window

Pass the last N chat messages as context to the AI so responses feel more situationally aware.

This requires:
1. A rolling `_chat_history` deque on `WebApp`
2. The deque is appended in `_dispatch`, snapshotted in `_route_ai`
3. `AIResponseHandler.handle()` / `_worker()` / `_query()` get a new `context` parameter
4. `_stream_openai` and `_stream_anthropic` take `user_content: str` instead of `username, message` separately

**Files:**
- Modify: `twitch_bot.py`
- Modify: `templates/index.html`

- [ ] **Step 1: Add config defaults**

```python
        # ── chat context ───────────────────────────────────────────────────────
        "ai_context_enabled": False,
        "ai_context_size":    5,
```

- [ ] **Step 2: Add `_chat_history` to `__init__`**

After `self._ai_counter = 0`:

```python
        self._chat_history: collections.deque[tuple[str, str]] = collections.deque(maxlen=20)
        self._history_lock  = threading.Lock()
```

- [ ] **Step 3: Load config in `__init__`**

```python
            "ai_context_enabled": settings.get("ai_context_enabled", False),
            "ai_context_size":    int(settings.get("ai_context_size", 5)),
```

- [ ] **Step 4: Persist in `_save_settings`**

```python
            "ai_context_enabled": c.get("ai_context_enabled", False),
            "ai_context_size":    c.get("ai_context_size",    5),
```

- [ ] **Step 5: Expose via API**

In `_SETTINGS_KEYS`:
```python
            "ai_context_enabled", "ai_context_size",
```

In `_BOOL_KEYS`:
```python
                "ai_context_enabled",
```

In `_INT_KEYS`:
```python
        _INT_KEYS = {"every_n", "min_bits", "thanks_cooldown_secs", "ai_context_size"}
```

- [ ] **Step 6: Append to history in `_dispatch`**

At the top of `_dispatch`, right after the ignore-list check (so ignored users are not logged to history):

```python
        with self._history_lock:
            self._chat_history.append((username, message))
```

- [ ] **Step 7: Snapshot history in `_route_ai` and pass to `ai.handle()`**

In `_route_ai`, add two new config reads inside the existing `with self._config_lock:` block:

```python
            context_enabled = self._config.get("ai_context_enabled", False)
            context_size    = int(self._config.get("ai_context_size", 5))
```

After the lock is released, before the trigger checks:

```python
        context: list[tuple[str, str]] | None = None
        if context_enabled and context_size > 0:
            with self._history_lock:
                hist = list(self._chat_history)
            # exclude the message we're about to process (it was just appended)
            if hist and hist[-1] == (username, message):
                hist = hist[:-1]
            context = hist[-context_size:] if hist else []
```

Change the final line of `_route_ai`:

```python
        ai = self._ai
        if triggered and ai:
            ai.handle(username, message, context=context)
```

- [ ] **Step 8: Add `context` parameter to `AIResponseHandler.handle()` and `_worker()`**

Change `handle()`:

```python
    def handle(self, username: str, message: str, reply_cb=None,
               prompt_override: str | None = None, use_tts: bool | None = None,
               context: list | None = None) -> None:
        self._q.put((username, message, reply_cb, prompt_override, use_tts, context))
```

Change `_worker()` unpack:

```python
            username, message, reply_cb, prompt_override, use_tts, context = item
```

And the `_query()` call:

```python
                self._query(username, message, reply_cb=reply_cb,
                            prompt_override=prompt_override, use_tts=use_tts,
                            context=context)
```

- [ ] **Step 9: Add `context` to `_query()` and build `user_content`**

Change `_query` signature:

```python
    def _query(self, username: str, message: str, reply_cb=None,
               prompt_override: str | None = None, use_tts: bool | None = None,
               context: list | None = None) -> None:
```

After reading `system_prompt` and `_use_tts` from config, add:

```python
        if context:
            ctx_lines    = "\n".join(f"{u}: {m}" for u, m in context)
            user_content = f"[Recent chat]\n{ctx_lines}\n\n{username}: {message}"
        else:
            user_content = f"{username}: {message}"
```

- [ ] **Step 10: Refactor `_stream_openai` and `_stream_anthropic` to take `user_content`**

Change `_stream_openai` signature (drop `username, message`, add `user_content`):

```python
    def _stream_openai(self, endpoint: str, model: str, api_key: str,
                       system_prompt: str, user_content: str,
                       tts_cb) -> str:
```

Inside `_stream_openai`, change the payload messages:

```python
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_content},
            ],
            "stream": True,
            "max_tokens": 1500,
        }
```

Change `_stream_anthropic` signature:

```python
    def _stream_anthropic(self, endpoint: str, model: str, api_key: str,
                          system_prompt: str, user_content: str,
                          tts_cb) -> str:
```

Inside `_stream_anthropic`, change the payload:

```python
        payload = {
            "model":      model,
            "max_tokens": 1500,
            "stream":     True,
            "system":     system_prompt,
            "messages":   [{"role": "user", "content": user_content}],
        }
```

Update the call sites in `_query`:

```python
        try:
            tts_cb = self.tts.speak if _use_tts else None
            if fmt == "anthropic":
                reply = self._stream_anthropic(endpoint, model, api_key, system_prompt, user_content, tts_cb)
            else:
                reply = self._stream_openai(endpoint, model, api_key, system_prompt, user_content, tts_cb)
```

- [ ] **Step 11: Add UI to AI tab**

In `#tab-ai`, after the Model dropdown row, add:

```html
        <div class="divider"></div>
        <div class="section-lbl">Chat Context</div>
        <label class="row-check">
          <input type="checkbox" id="s-ctx-enabled" onchange="toggleCtx()">
          Include recent chat history in AI prompt
        </label>
        <div id="s-ctx-wrap" style="display:none">
          <div class="field-row" style="margin-top:6px">
            <label>Messages to include</label>
            <input type="number" id="s-ctx-size" min="1" max="20" style="width:60px">
          </div>
          <div class="hint">The last N chat messages are prepended to the AI's context window.</div>
        </div>
```

Add toggle function:

```javascript
function toggleCtx() {
  el('s-ctx-wrap').style.display = el('s-ctx-enabled').checked ? '' : 'none';
}
```

In `openSettings()`:

```javascript
    el('s-ctx-enabled').checked = !!s.ai_context_enabled;
    el('s-ctx-size').value      = s.ai_context_size ?? 5;
    toggleCtx();
```

In `saveSettings()`:

```javascript
    ai_context_enabled: el('s-ctx-enabled').checked,
    ai_context_size:    parseInt(el('s-ctx-size').value) || 5,
```

- [ ] **Step 12: Verify**

Enable context with size 3. Send 4 messages in chat. Trigger an AI response. In the console you should see `[AI] →` with a reply that references the prior conversation. Confirm the bot still works without context enabled (existing behavior unchanged).

- [ ] **Step 13: Commit**

```bash
git add twitch_bot.py templates/index.html
git commit -m "feat(ai): add rolling chat context window passed to LLM for situational awareness"
```

---

## Self-Review

**Spec coverage:**
- ✅ Ignore list — Task 1
- ✅ Thanks cooldown — Task 2
- ✅ Custom !commands — Task 3
- ✅ Scheduled messages — Task 4
- ✅ Chat context window — Task 5

**Placeholder scan:** No TBDs or "implement later" items found. All code blocks are complete.

**Type consistency:**
- `context: list | None` used consistently across `handle`, `_worker`, `_query`
- `user_content: str` replaces `username, message` in both stream methods
- `scheduled_msgs: list[dict]` with `{"text": str, "interval": int}` shape consistent across defaults, load, save, and API handler
- `chat_commands: dict[str, str]` consistent across all layers
- `ignore_list: list[str]` consistent across all layers

**Notes for implementer:**
- Tasks must be done in order — each modifies the same file and later tasks reference code from earlier ones
- The `_history_lock` in Task 5 must not be acquired while `_config_lock` is held (and vice versa) — the ordering in the plan avoids this
- The `escHtml` JS helper added in Task 3 is reused by Task 4's `addSchedRow` — ensure Task 3 runs first
- The `_INT_KEYS` set grows across tasks — each task shows the complete updated set
