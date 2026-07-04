# Thank-You Responses Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect Twitch channel events (subs, resubs, gifted subs, mystery gifts, bits cheers, raids) via IRC USERNOTICE and respond with AI-generated thank-you messages posted to Twitch chat and/or spoken via TTS, all configurable via a new "Thanks" settings tab.

**Architecture:** `TwitchIRCClient._handle` gains a USERNOTICE parsing block that fires an `on_event(event_type, username, extra)` callback. `TwitchBotApp._handle_event` receives this, checks per-event toggles from config, builds an event description string, and passes it to the existing `AIResponseHandler.handle()` with a `prompt_override` (separate thanks prompt) and a `reply_cb` that posts the reply to Twitch chat via the existing `say()` method. Bits cheers piggyback on the existing `_dispatch` flow. The feature is fully configurable via a new "Thanks" tab in the settings modal.

**Tech Stack:** Python (existing `TwitchIRCClient`, `AIResponseHandler`, `TwitchBotApp`), Flask, vanilla JS, HTML/CSS in `templates/index.html`. No new dependencies.

---

## File Map

- **Modify:** `twitch_bot.py` — all Python changes (IRC, app logic, API routes, config)
- **Modify:** `templates/index.html` — new "Thanks" tab UI + JS

---

## Task 1: IRC layer — USERNOTICE parsing and `on_event` callback

**Files:**
- Modify: `twitch_bot.py` (lines ~556–674, `TwitchIRCClient.__init__` and `_handle`)

- [ ] **Step 1: Add `on_event` attribute to `TwitchIRCClient.__init__`**

  In `__init__` (currently ends around line 564), add `on_event` after the existing attributes:

  ```python
  def __init__(self, get_creds, log, on_message, on_ready=None, on_reconnecting=None) -> None:
      self.get_creds       = get_creds
      self.log             = log
      self.on_message      = on_message
      self.on_ready        = on_ready
      self.on_reconnecting = on_reconnecting
      self.on_event: callable | None = None   # ← add this line
      self._sock: socket.socket | None = None
      self._running = False
      self._ready_fired = False
  ```

- [ ] **Step 2: Add USERNOTICE parsing to `TwitchIRCClient._handle`**

  After the existing PRIVMSG block (after line 674, which is `self.on_message(m.group(1), m.group(2).strip(), bits, reward_id)`), add:

  ```python
  if " USERNOTICE #" in line:
      msg_id   = tags.get("msg-id", "")
      username = tags.get("display-name") or tags.get("login", "")
      event_type: str | None = None
      extra: dict = {}
      if msg_id == "sub":
          event_type = "sub"
          extra["plan"] = tags.get("msg-param-sub-plan", "")
      elif msg_id == "resub":
          event_type = "resub"
          extra["months"] = tags.get("msg-param-cumulative-months", "1")
          extra["streak"] = tags.get("msg-param-streak-months", "0")
          extra["plan"]   = tags.get("msg-param-sub-plan", "")
      elif msg_id == "subgift":
          event_type = "subgift"
          extra["recipient"] = tags.get("msg-param-recipient-display-name", "a viewer")
          extra["plan"]      = tags.get("msg-param-sub-plan", "")
      elif msg_id == "submysterygift":
          event_type = "mysterygift"
          extra["count"] = tags.get("msg-param-mass-gift-count", "1")
      elif msg_id == "raid":
          event_type = "raid"
          extra["viewers"] = tags.get("msg-param-viewerCount", "0")
      if event_type and username and self.on_event:
          self.on_event(event_type, username, extra)
  ```

- [ ] **Step 3: Verify the full `_handle` method looks correct**

  Run: `python -c "import twitch_bot"` from the project directory.
  Expected: no output (import succeeds with no syntax errors).

- [ ] **Step 4: Commit**

  ```bash
  git add twitch_bot.py
  git commit -m "feat(irc): parse USERNOTICE events and fire on_event callback"
  ```

---

## Task 2: App layer — config defaults, `_handle_event`, and bits hook

**Files:**
- Modify: `twitch_bot.py` (`_DEFAULT_THANKS_PROMPT` constant, `_SETTINGS_DEFAULTS`, `__init__` config block, `_save_settings`, `_load_settings` is handled automatically by update to defaults, `_connect`, `_dispatch`, new `_handle_event` method)

- [ ] **Step 1: Add `_DEFAULT_THANKS_PROMPT` module-level constant**

  After the `_CLAUDE_MODELS` list (around line 85), add:

  ```python
  _DEFAULT_THANKS_PROMPT = (
      "You are a friendly Twitch streamer's bot. When a viewer subs, resubs, gifts subs, "
      "cheers bits, or raids, respond with a warm, brief, personalized thank-you message "
      "that fits naturally in Twitch chat. Keep it under two sentences. Do not use hashtags."
  )
  ```

- [ ] **Step 2: Add thanks keys to `_SETTINGS_DEFAULTS`**

  `_SETTINGS_DEFAULTS` is a class attribute on `TwitchBotApp` (around line 858). Add the new keys:

  ```python
  _SETTINGS_DEFAULTS: dict = {
      "ai_enabled":        False,
      "trigger_every_n":   True,
      "every_n":           5,
      "trigger_mentions":  False,
      "trigger_bits":      False,
      "min_bits":          100,
      "trigger_points":    False,
      "reward_id":         "",
      "tts_ai":            True,
      "plays_enabled":     False,
      "command_map":       {},
      "last_prompt":       "",
      # ── thank-you responses ────────────────────────────────────────────────
      "thanks_enabled": False,
      "thanks_sub":     True,
      "thanks_resub":   True,
      "thanks_gift":    True,
      "thanks_mystery": True,
      "thanks_bits":    False,
      "thanks_raid":    True,
      "thanks_chat":    True,
      "thanks_tts":     True,
      "thanks_prompt":  "",
  }
  ```

- [ ] **Step 3: Add thanks keys to `self._config` in `__init__`**

  In `__init__`, after the existing `"last_prompt"` entry and the `"system_prompt": ""` line (around line 927), add the thanks config keys. Insert them in the `# ── runtime toggles` section:

  ```python
  "thanks_enabled":   settings.get("thanks_enabled",  False),
  "thanks_sub":       settings.get("thanks_sub",       True),
  "thanks_resub":     settings.get("thanks_resub",     True),
  "thanks_gift":      settings.get("thanks_gift",      True),
  "thanks_mystery":   settings.get("thanks_mystery",   True),
  "thanks_bits":      settings.get("thanks_bits",      False),
  "thanks_raid":      settings.get("thanks_raid",      True),
  "thanks_chat":      settings.get("thanks_chat",      True),
  "thanks_tts":       settings.get("thanks_tts",       True),
  "thanks_prompt":    settings.get("thanks_prompt",    ""),
  ```

- [ ] **Step 4: Add thanks keys to `_save_settings`**

  In `_save_settings` (around line 1121), add thanks keys to the `data` dict written to JSON:

  ```python
  data = {
      "ai_enabled":       c.get("ai_enabled",       False),
      "trigger_every_n":  c.get("trigger_every_n",  True),
      "every_n":          c.get("every_n",          5),
      "trigger_mentions": c.get("trigger_mentions", False),
      "trigger_bits":     c.get("trigger_bits",     False),
      "min_bits":         c.get("min_bits",         100),
      "trigger_points":   c.get("trigger_points",   False),
      "reward_id":        c.get("reward_id",        ""),
      "tts_ai":           c.get("tts_ai",           True),
      "plays_enabled":    c.get("plays_enabled",    False),
      "command_map":      c.get("command_map",      {}),
      "last_prompt":      c.get("last_prompt",      ""),
      # ── thank-you responses ────────────────────────────────────────
      "thanks_enabled":   c.get("thanks_enabled",   False),
      "thanks_sub":       c.get("thanks_sub",       True),
      "thanks_resub":     c.get("thanks_resub",     True),
      "thanks_gift":      c.get("thanks_gift",      True),
      "thanks_mystery":   c.get("thanks_mystery",   True),
      "thanks_bits":      c.get("thanks_bits",      False),
      "thanks_raid":      c.get("thanks_raid",      True),
      "thanks_chat":      c.get("thanks_chat",      True),
      "thanks_tts":       c.get("thanks_tts",       True),
      "thanks_prompt":    c.get("thanks_prompt",    ""),
  }
  ```

- [ ] **Step 5: Add `_handle_event` method to `TwitchBotApp`**

  Add this method to `TwitchBotApp`, near the other dispatch methods (after `_route_ai`, around line 1715):

  ```python
  def _handle_event(self, event_type: str, username: str, extra: dict) -> None:
      with self._config_lock:
          enabled   = self._config.get("thanks_enabled", False)
          event_map = {
              "sub":         self._config.get("thanks_sub",     True),
              "resub":       self._config.get("thanks_resub",   True),
              "subgift":     self._config.get("thanks_gift",    True),
              "mysterygift": self._config.get("thanks_mystery", True),
              "bits":        self._config.get("thanks_bits",    False),
              "raid":        self._config.get("thanks_raid",    True),
          }
          chat_on = self._config.get("thanks_chat",    True)
          tts_on  = self._config.get("thanks_tts",     True)
          prompt  = self._config.get("thanks_prompt",  "") or _DEFAULT_THANKS_PROMPT
          channel = self._config.get("twitch_channel", "").lower().strip()

      if not enabled or not event_map.get(event_type, False):
          return

      ai = self._ai
      if not ai:
          return

      _templates = {
          "sub":         lambda u, e: f"[EVENT] {u} just subscribed! Thank them warmly.",
          "resub":       lambda u, e: (
              f"[EVENT] {u} resubscribed for {e.get('months','?')} months "
              f"({e.get('streak','0')} month streak)! Thank them."
          ),
          "subgift":     lambda u, e: f"[EVENT] {u} gifted a sub to {e.get('recipient','a viewer')}! Thank {u}.",
          "mysterygift": lambda u, e: f"[EVENT] {u} gifted {e.get('count','?')} subs to the community! Thank them.",
          "raid":        lambda u, e: f"[EVENT] {u} raided with {e.get('viewers','?')} viewers! Welcome them and their community.",
          "bits":        lambda u, e: f"[EVENT] {u} cheered {e.get('bits','?')} bits! Thank them.",
      }
      if event_type not in _templates:
          return
      msg = _templates[event_type](username, extra)
      self._log(f"[Thanks] {event_type} from {username}")

      def reply_cb(reply: str) -> None:
          self._log(f"[Thanks] → {reply}")
          if chat_on:
              irc = self._irc
              if irc and channel:
                  irc.say(channel, reply)

      ai.handle(username, msg, reply_cb=reply_cb, prompt_override=prompt, use_tts=tts_on)
  ```

- [ ] **Step 6: Wire `on_event` in `_connect`**

  In `_connect` (around line 1566–1573), after `self._irc = TwitchIRCClient(...)` and before `self._irc.connect()`, add:

  ```python
  self._irc.on_event = self._handle_event
  ```

  The full `_connect` block should look like:

  ```python
  self._irc = TwitchIRCClient(
      get_creds=self._get_irc_creds,
      log=self._log,
      on_message=self._dispatch,
      on_ready=self._on_irc_ready,
      on_reconnecting=self._on_irc_reconnecting,
  )
  self._irc.on_event = self._handle_event
  self._irc.connect()
  ```

- [ ] **Step 7: Add bits cheer hook in `_dispatch`**

  In `_dispatch` (around line 1645), after the existing `self._route_ai(username, message, bits, reward_id)` call, add:

  ```python
  if bits > 0:
      self._handle_event("bits", username, {"bits": bits})
  ```

- [ ] **Step 8: Verify import still works**

  Run: `python -c "import twitch_bot"` from the project directory.
  Expected: no output.

- [ ] **Step 9: Commit**

  ```bash
  git add twitch_bot.py
  git commit -m "feat(app): add _handle_event, thanks config defaults, and bits hook"
  ```

---

## Task 3: API routes — expose thanks settings to the web UI

**Files:**
- Modify: `twitch_bot.py` (`_register_routes` → `_SETTINGS_KEYS`, `api_settings_post` `_BOOL_KEYS`, `api_state`)

- [ ] **Step 1: Add thanks keys to `_SETTINGS_KEYS`**

  In `_register_routes` (around line 1320), extend `_SETTINGS_KEYS`:

  ```python
  _SETTINGS_KEYS = (
      "twitch_channel", "twitch_username", "twitch_client_id", "twitch_token",
      "llm_provider", "llm_endpoint", "llm_model", "llm_api_key",
      "piper_exe", "piper_model", "piper_config",
      "discord_token", "discord_channel_id", "discord_trigger",
      "discord_use_shared_prompt", "discord_prompt",
      "trigger_every_n", "every_n", "trigger_mentions", "trigger_bits",
      "min_bits", "trigger_points", "reward_id", "tts_ai",
      # ── thank-you responses ──────────────────────────────────────────
      "thanks_enabled", "thanks_sub", "thanks_resub", "thanks_gift",
      "thanks_mystery", "thanks_bits", "thanks_raid", "thanks_chat", "thanks_tts",
      "thanks_prompt",
  )
  ```

- [ ] **Step 2: Add thanks bool keys to `_BOOL_KEYS` in the POST handler**

  In `api_settings_post` (around line 1340), extend `_BOOL_KEYS`:

  ```python
  _BOOL_KEYS = {
      "trigger_every_n", "trigger_mentions", "trigger_bits",
      "trigger_points", "tts_ai", "discord_use_shared_prompt",
      "thanks_enabled", "thanks_sub", "thanks_resub", "thanks_gift",
      "thanks_mystery", "thanks_bits", "thanks_raid", "thanks_chat", "thanks_tts",
  }
  ```

- [ ] **Step 3: Verify import and basic API logic**

  Run: `python -c "import twitch_bot"` from the project directory.
  Expected: no output.

- [ ] **Step 4: Commit**

  ```bash
  git add twitch_bot.py
  git commit -m "feat(api): expose thanks settings via /api/settings GET and POST"
  ```

---

## Task 4: UI — "Thanks" tab in settings modal

**Files:**
- Modify: `templates/index.html`

- [ ] **Step 1: Add "Thanks" tab button**

  In the `.tab-bar` div of `#settings-modal` (around line 244–248), add a fifth tab button:

  Replace:
  ```html
  <div class="tab-bar">
    <button class="tab active" onclick="showTab('twitch')">Twitch</button>
    <button class="tab"        onclick="showTab('ai')">AI</button>
    <button class="tab"        onclick="showTab('discord')">Discord</button>
    <button class="tab"        onclick="showTab('tts')">TTS</button>
  </div>
  ```

  With:
  ```html
  <div class="tab-bar">
    <button class="tab active" onclick="showTab('twitch')">Twitch</button>
    <button class="tab"        onclick="showTab('ai')">AI</button>
    <button class="tab"        onclick="showTab('discord')">Discord</button>
    <button class="tab"        onclick="showTab('tts')">TTS</button>
    <button class="tab"        onclick="showTab('thanks')">Thanks</button>
  </div>
  ```

- [ ] **Step 2: Add "Thanks" tab pane**

  After the closing `</div>` of the TTS tab pane (around line 342) and before `</div><!-- /modal-body -->`, add:

  ```html
      <!-- Thanks tab -->
      <div id="tab-thanks" class="tab-pane">
        <div class="section-lbl">Enable</div>
        <label class="row-check">
          <input type="checkbox" id="s-thanks-enabled">
          Enable thank-you responses
        </label>

        <div class="divider"></div>
        <div class="section-lbl">Events</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px">
          <label class="row-check"><input type="checkbox" id="s-thanks-sub"> New sub</label>
          <label class="row-check"><input type="checkbox" id="s-thanks-resub"> Resub</label>
          <label class="row-check"><input type="checkbox" id="s-thanks-gift"> Gifted sub</label>
          <label class="row-check"><input type="checkbox" id="s-thanks-mystery"> Mystery gifts</label>
          <label class="row-check"><input type="checkbox" id="s-thanks-bits"> Bits cheer</label>
          <label class="row-check"><input type="checkbox" id="s-thanks-raid"> Raid</label>
        </div>

        <div class="divider"></div>
        <div class="section-lbl">Delivery</div>
        <label class="row-check"><input type="checkbox" id="s-thanks-chat"> Post to Twitch chat</label>
        <label class="row-check"><input type="checkbox" id="s-thanks-tts"> Speak via TTS</label>

        <div class="divider"></div>
        <div class="section-lbl">Thank-you prompt</div>
        <textarea id="s-thanks-prompt" rows="5" placeholder="You are a friendly Twitch streamer&#39;s bot. Thank viewers warmly and briefly…"></textarea>
        <div class="hint">Leave blank to use the built-in default prompt.</div>
      </div>
  ```

- [ ] **Step 3: Populate thanks fields in `openSettings()`**

  In the `openSettings()` function (around line 759), after the existing field assignments (after `toggleDiscordPrompt();`), add:

  ```javascript
  el('s-thanks-enabled').checked  = !!s.thanks_enabled;
  el('s-thanks-sub').checked      = (s.thanks_sub !== false);
  el('s-thanks-resub').checked    = (s.thanks_resub !== false);
  el('s-thanks-gift').checked     = (s.thanks_gift !== false);
  el('s-thanks-mystery').checked  = (s.thanks_mystery !== false);
  el('s-thanks-bits').checked     = !!s.thanks_bits;
  el('s-thanks-raid').checked     = (s.thanks_raid !== false);
  el('s-thanks-chat').checked     = (s.thanks_chat !== false);
  el('s-thanks-tts').checked      = (s.thanks_tts !== false);
  el('s-thanks-prompt').value     = s.thanks_prompt || '';
  ```

- [ ] **Step 4: Include thanks fields in `saveSettings()`**

  In `saveSettings()` (around line 805), add thanks keys to the `body` object:

  ```javascript
  function saveSettings() {
    const body = {
      twitch_channel:            el('s-channel').value.trim(),
      twitch_username:           el('s-username').value.trim(),
      twitch_client_id:          el('s-client-id').value.trim(),
      twitch_token:              el('s-token').value.trim(),
      llm_provider:              el('s-provider').value,
      llm_endpoint:              el('s-endpoint').value.trim(),
      llm_model:                 el('s-model').value,
      llm_api_key:               el('s-api-key').value.trim(),
      piper_exe:                 el('s-piper-exe').value.trim(),
      piper_model:               el('s-piper-model').value.trim(),
      piper_config:              el('s-piper-cfg').value.trim(),
      discord_token:             el('s-discord-token').value.trim(),
      discord_channel_id:        el('s-discord-channel').value.trim(),
      discord_trigger:           el('s-discord-trigger').value,
      discord_use_shared_prompt: el('s-discord-shared').checked,
      discord_prompt:            el('s-discord-prompt').value,
      thanks_enabled:            el('s-thanks-enabled').checked,
      thanks_sub:                el('s-thanks-sub').checked,
      thanks_resub:              el('s-thanks-resub').checked,
      thanks_gift:               el('s-thanks-gift').checked,
      thanks_mystery:            el('s-thanks-mystery').checked,
      thanks_bits:               el('s-thanks-bits').checked,
      thanks_raid:               el('s-thanks-raid').checked,
      thanks_chat:               el('s-thanks-chat').checked,
      thanks_tts:                el('s-thanks-tts').checked,
      thanks_prompt:             el('s-thanks-prompt').value,
    };
    api('/api/settings','POST', body).then(() => closeSettings());
  }
  ```

- [ ] **Step 5: Start the server and verify the Thanks tab appears**

  Run: `.venv/bin/python twitch_bot.py` and open `http://localhost:5000` in a browser.

  - Click the ⚙ gear button to open Settings.
  - Confirm a "Thanks" tab appears alongside Twitch / AI / Discord / TTS.
  - Click the Thanks tab — confirm all checkboxes and the prompt textarea render correctly.
  - Check "Enable thank-you responses" + a few event checkboxes, fill in a prompt, click **Save Settings**.
  - Reopen Settings → Thanks tab — confirm values persisted.

  Expected: no errors in the server console, values survive a save/reload cycle.

- [ ] **Step 6: Commit**

  ```bash
  git add templates/index.html
  git commit -m "feat(ui): add Thanks tab to settings modal with event/delivery toggles and prompt"
  ```

---

## Task 5: End-to-end smoke test and push

- [ ] **Step 1: Manual event simulation test**

  With the server running and connected to Twitch, open the browser console on the web UI. Confirm `[Thanks]` log lines appear when the AI responds to thank-you events. If you have access to a test channel, trigger a sub/raid to verify the full flow end-to-end.

  Alternatively, test `_handle_event` directly by adding a temporary route to POST a fake event — but the code path is straightforward enough that import + visual inspection of the Thanks tab covers the core risk.

- [ ] **Step 2: Verify no regression on existing flows**

  - Connect to Twitch IRC — confirm regular chat messages still route through `_dispatch` → `_route_ai` as before.
  - Send a manual AI message from the UI — confirm it works.
  - Confirm the existing Twitch / AI / Discord / TTS tabs in Settings still save correctly.

- [ ] **Step 3: Push**

  ```bash
  git push
  ```
