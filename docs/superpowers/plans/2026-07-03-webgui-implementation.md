# Web GUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the CustomTkinter desktop GUI with a Flask + SSE + vanilla-JS web UI so the bot runs headless and is accessible across the local network.

**Architecture:** `TwitchBotApp` (lines 748–2191 of `twitch_bot.py`) is deleted and replaced by a `WebApp` class. The five service classes (GameInputController, TTSEngine, AIResponseHandler, TwitchIRCClient, DiscordClient) are completely unchanged. A new `templates/index.html` carries the entire frontend.

**Tech Stack:** Python 3.11+, Flask 3.x, SSE (text/event-stream), vanilla JS (no build step), existing service classes unchanged.

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `twitch_bot.py` | Modify (lines 748–2191 replaced) | Delete `TwitchBotApp`, add `WebApp` + Flask routes |
| `templates/index.html` | Create | Full SPA: HTML + embedded CSS + JS |
| `requirements.txt` | Modify | Remove `customtkinter`, add `flask` |
| `tests/test_discord_integration.py` | Modify | Remove `customtkinter` mock (no longer needed) |

---

## Task 1: Install Flask and update requirements.txt

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Update requirements.txt**

Replace the file content with:

```
# HTTP server for web UI
flask>=3.0.0

# HTTP client for local LLM (Ollama / LM Studio)
requests>=2.31.0

# Audio playback (TTS output)
pygame>=2.5.0

# Game input simulation
pydirectinput>=1.0.4
pynput>=1.7.6

# Discord bot integration
discord.py>=2.3.0
```

- [ ] **Step 2: Install flask into the existing venv**

```bash
.venv/bin/pip install flask>=3.0.0
```

Expected output ends with: `Successfully installed flask-...`

- [ ] **Step 3: Verify installation**

```bash
.venv/bin/python -c "import flask; print(flask.__version__)"
```

Expected: prints a version number like `3.x.x`

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "deps: replace customtkinter with flask for web UI"
```

---

## Task 2: Add `_BoolGetter` helper and remove CTk imports from `twitch_bot.py`

**Files:**
- Modify: `twitch_bot.py` (top of file, lines 28–70)

The current imports include `customtkinter`, `tkinter.filedialog`, and `ctk.set_appearance_mode(...)`. These must all go. Add one small helper class.

- [ ] **Step 1: Replace the imports block (lines 28–70)**

Find and replace the entire block from `from __future__ import annotations` down through `ctk.set_default_color_theme("blue")`. The new block is:

```python
from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import queue
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime

import flask as _flask
import requests

# ── Optional dependencies (graceful degradation) ────────────────────────────
try:
    import pygame
    pygame.mixer.init(frequency=22050, size=-16, channels=1, buffer=512)
    HAS_PYGAME = True
except Exception:
    HAS_PYGAME = False

try:
    import pydirectinput
    pydirectinput.PAUSE = 0.03
    HAS_PYDIRECTINPUT = True
except Exception:
    HAS_PYDIRECTINPUT = False

try:
    from pynput.keyboard import Controller as _KBCtrl
    _pynput_kb = _KBCtrl()
    HAS_PYNPUT = True
except Exception:
    HAS_PYNPUT = False
```

- [ ] **Step 2: Remove the colour constants block (the four lines `_GREEN`, `_RED`, `ON_FG`, `OFF_FG`)**

These are CTk-specific and unused in the web version. Delete them:

```python
# DELETE these four lines:
_GREEN = ("#1a7f37", "#15662d")
_RED   = ("#c0392b", "#922b21")
ON_FG  = "#2ecc71"
OFF_FG = "#e74c3c"
```

- [ ] **Step 3: Add `_BoolGetter` helper after `_CLAUDE_MODELS` list (after line ~100)**

`GameInputController.__init__` expects an object with a `.get()` method (was `ctk.BooleanVar`). Add this minimal wrapper right after the `_CLAUDE_MODELS` list:

```python
class _BoolGetter:
    """Minimal stand-in for ctk.BooleanVar used by GameInputController."""
    __slots__ = ("_v",)
    def __init__(self, v: bool) -> None: self._v = v
    def get(self) -> bool: return self._v
```

- [ ] **Step 4: Verify no ctk references remain above the TwitchBotApp class**

```bash
grep -n "ctk\|customtkinter\|tkinter" twitch_bot.py | head -30
```

Expected: only lines inside the old `TwitchBotApp` class (those will be deleted in Task 3).

- [ ] **Step 5: Commit**

```bash
git add twitch_bot.py
git commit -m "refactor: remove customtkinter imports, add _BoolGetter helper"
```

---

## Task 3: Replace `TwitchBotApp` with `WebApp` — core init and config

**Files:**
- Modify: `twitch_bot.py` (lines 748–2191: delete `TwitchBotApp` class and old entry point, write `WebApp`)

This is the largest task. Do it in several steps.

- [ ] **Step 1: Delete everything from `class TwitchBotApp` to the end of the file**

In your editor, select from line 748 (`class TwitchBotApp(ctk.CTk):`) through line 2191 (end of file) and delete it.

- [ ] **Step 2: Paste the `WebApp` class `__init__` at the end of the file**

```python
# ══════════════════════════════════════════════════════════════════════════════
# WebApp  (replaces TwitchBotApp)
# ══════════════════════════════════════════════════════════════════════════════
class WebApp:
    """
    Owns all service instances and the Flask web server.
    All state lives in _config (protected by _config_lock).
    Background threads communicate via _log() which fans out to SSE clients.
    """

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
    }

    def __init__(self) -> None:
        _here = os.path.dirname(os.path.abspath(__file__))
        self._here           = _here
        self._prompts_dir    = os.path.join(_here, "prompts")
        self._presets_dir    = os.path.join(_here, "plays_presets")
        self._env_path       = os.path.join(_here, ".env")
        self._settings_path  = os.path.join(_here, "settings.json")
        os.makedirs(self._prompts_dir, exist_ok=True)
        os.makedirs(self._presets_dir, exist_ok=True)

        env      = self._load_env()
        settings = self._load_settings()

        _local_piper   = os.path.join(_here, "piper", "piper")
        _default_piper = _local_piper if os.path.exists(_local_piper) else ""

        # Single source of truth for all runtime config.
        # Read from worker threads via get_*_cfg() callables (which snapshot under lock).
        self._config: dict = {
            # ── credentials (.env) ─────────────────────────────────────────
            "twitch_channel":          env.get("TWITCH_CHANNEL", ""),
            "twitch_username":         env.get("TWITCH_USERNAME", ""),
            "twitch_client_id":        env.get("TWITCH_CLIENT_ID", ""),
            "twitch_token":            env.get("TWITCH_TOKEN", ""),
            "llm_provider":            env.get("LLM_PROVIDER", "Ollama"),
            "llm_endpoint":            env.get("LLM_ENDPOINT",
                                               "http://localhost:11434/v1/chat/completions"),
            "llm_model":               env.get("LLM_MODEL", "llama3"),
            "llm_api_key":             env.get("LLM_API_KEY", ""),
            "piper_exe":               env.get("PIPER_EXE", "") or _default_piper,
            "piper_model":             env.get("PIPER_MODEL", ""),
            "piper_config":            env.get("PIPER_CONFIG", ""),
            "discord_token":           env.get("DISCORD_TOKEN", ""),
            "discord_channel_id":      env.get("DISCORD_CHANNEL_ID", ""),
            "discord_trigger":         env.get("DISCORD_TRIGGER", "All messages"),
            "discord_use_shared_prompt": (
                env.get("DISCORD_USE_SHARED_PROMPT", "true").lower() != "false"
            ),
            "discord_prompt":          env.get("DISCORD_PROMPT", ""),
            "web_port":                int(env.get("WEB_PORT", "5000") or "5000"),
            # ── runtime toggles (settings.json) ────────────────────────────
            "ai_enabled":       settings.get("ai_enabled",       False),
            "plays_enabled":    settings.get("plays_enabled",    False),
            "trigger_every_n":  settings.get("trigger_every_n",  True),
            "every_n":          settings.get("every_n",          5),
            "trigger_mentions": settings.get("trigger_mentions", False),
            "trigger_bits":     settings.get("trigger_bits",     False),
            "min_bits":         settings.get("min_bits",         100),
            "trigger_points":   settings.get("trigger_points",   False),
            "reward_id":        settings.get("reward_id",        ""),
            "tts_ai":           settings.get("tts_ai",           True),
            "command_map":      dict(settings.get("command_map", {})),
            "last_prompt":      settings.get("last_prompt",      ""),
            "system_prompt":    "",
            # ── connection status ───────────────────────────────────────────
            "twitch_status":    "off",   # off / connecting / online
            "discord_status":   "off",   # off / connecting / online / error
        }
        self._config_lock = threading.Lock()
        self._ai_counter  = 0

        # Load last-used prompt content from file
        last = self._config["last_prompt"]
        if last:
            _p = os.path.join(self._prompts_dir, f"{last}.txt")
            if os.path.exists(_p):
                try:
                    with open(_p, encoding="utf-8") as _f:
                        self._config["system_prompt"] = _f.read()
                except Exception:
                    pass

        # SSE log broadcast: _log_lock protects both _log_ring and _sse_clients
        self._log_lock:   threading.Lock        = threading.Lock()
        self._log_ring:   collections.deque     = collections.deque(maxlen=200)
        self._sse_clients: list[queue.Queue]    = []

        # Service handles
        self._irc:     TwitchIRCClient | None   = None
        self._tts:     TTSEngine | None         = None
        self._ai:      AIResponseHandler | None = None
        self._discord: DiscordClient | None     = None

        # Flask app
        self._flask = _flask.Flask(
            __name__,
            template_folder=os.path.join(_here, "templates"),
        )
        self._register_routes()

        self._start_services()
        self._log("[System] Ready.")
        self._log_platform_info()
        self._autosave()

        # Auto-connect if credentials already saved
        if all(self._config.get(k) for k in
               ("twitch_channel", "twitch_username", "twitch_token")):
            t = threading.Timer(0.8, self._connect)
            t.daemon = True
            t.start()
        if self._config.get("discord_token") and self._config.get("discord_channel_id"):
            t = threading.Timer(1.2, self._discord_connect)
            t.daemon = True
            t.start()
```

- [ ] **Step 3: Add platform diagnostics method**

Append immediately after `__init__`:

```python
    def _log_platform_info(self) -> None:
        libs = []
        libs.append("pygame ✓" if HAS_PYGAME else "pygame ✗ (no audio)")
        libs.append("pydirectinput ✓" if HAS_PYDIRECTINPUT else "pydirectinput ✗")
        libs.append("pynput ✓" if HAS_PYNPUT else "pynput ✗")
        self._log(f"[System] Libraries: {', '.join(libs)}")
        if not HAS_PYDIRECTINPUT and not HAS_PYNPUT:
            self._log("[System] WARNING: No input library found — Twitch Plays disabled.")
```

- [ ] **Step 4: Add logging and broadcast methods**

```python
    # ══════════════════════════════════════════════════════════════════════════
    # Thread-safe logging + SSE broadcast
    # ══════════════════════════════════════════════════════════════════════════

    def _log(self, msg: str) -> None:
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}]  {msg}"
        with self._log_lock:
            self._log_ring.append(line)
            for q in list(self._sse_clients):
                q.put(f"data: {line}\n\n")

    def _broadcast_status(self) -> None:
        """Push a status event to all SSE clients (called after connection changes)."""
        with self._config_lock:
            payload = json.dumps({
                "twitch_status":  self._config["twitch_status"],
                "discord_status": self._config["discord_status"],
            })
        msg = f"event: status\ndata: {payload}\n\n"
        with self._log_lock:
            for q in list(self._sse_clients):
                q.put(msg)
```

- [ ] **Step 5: Add config getter callables (used by worker threads)**

```python
    # ══════════════════════════════════════════════════════════════════════════
    # Config getters — called on worker threads; snapshot under lock
    # ══════════════════════════════════════════════════════════════════════════

    def _get_tts_cfg(self) -> dict:
        with self._config_lock:
            return {
                "piper_exe":   self._config.get("piper_exe")    or "piper",
                "model_path":  self._config.get("piper_model",  ""),
                "config_path": self._config.get("piper_config", ""),
            }

    def _get_ai_cfg(self) -> dict:
        with self._config_lock:
            return {
                "provider":      self._config.get("llm_provider", "Ollama"),
                "endpoint":      self._config.get("llm_endpoint", ""),
                "model":         self._config.get("llm_model",    ""),
                "api_key":       self._config.get("llm_api_key",  ""),
                "system_prompt": self._config.get("system_prompt", ""),
                "tts_ai":        self._config.get("tts_ai",        True),
            }

    def _get_irc_creds(self) -> dict:
        with self._config_lock:
            return {
                "channel":  self._config.get("twitch_channel",  ""),
                "username": self._config.get("twitch_username", ""),
                "token":    self._config.get("twitch_token",    ""),
            }

    def _get_discord_cfg(self) -> dict:
        with self._config_lock:
            use_shared    = self._config.get("discord_use_shared_prompt", True)
            discord_prompt = "" if use_shared else self._config.get("discord_prompt", "")
            return {
                "discord_token":      self._config.get("discord_token",      ""),
                "discord_channel_id": self._config.get("discord_channel_id", ""),
                "discord_trigger":    self._config.get("discord_trigger",    "All messages"),
                "discord_prompt":     discord_prompt,
            }
```

- [ ] **Step 6: Add persistence methods**

```python
    # ══════════════════════════════════════════════════════════════════════════
    # Persistence
    # ══════════════════════════════════════════════════════════════════════════

    def _load_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if not os.path.exists(self._env_path):
            return env
        with open(self._env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
        return env

    def _save_env(self) -> None:
        with self._config_lock:
            c = dict(self._config)
        lines = [
            f"TWITCH_CHANNEL={c.get('twitch_channel', '')}",
            f"TWITCH_USERNAME={c.get('twitch_username', '')}",
            f"TWITCH_CLIENT_ID={c.get('twitch_client_id', '')}",
            f"TWITCH_TOKEN={c.get('twitch_token', '')}",
            f"LLM_PROVIDER={c.get('llm_provider', 'Ollama')}",
            f"LLM_ENDPOINT={c.get('llm_endpoint', '')}",
            f"LLM_MODEL={c.get('llm_model', '')}",
            f"LLM_API_KEY={c.get('llm_api_key', '')}",
            f"PIPER_EXE={c.get('piper_exe', '')}",
            f"PIPER_MODEL={c.get('piper_model', '')}",
            f"PIPER_CONFIG={c.get('piper_config', '')}",
            f"DISCORD_TOKEN={c.get('discord_token', '')}",
            f"DISCORD_CHANNEL_ID={c.get('discord_channel_id', '')}",
            f"DISCORD_TRIGGER={c.get('discord_trigger', 'All messages')}",
            f"DISCORD_USE_SHARED_PROMPT="
            f"{'true' if c.get('discord_use_shared_prompt', True) else 'false'}",
            f"DISCORD_PROMPT={c.get('discord_prompt', '')}",
            f"WEB_PORT={c.get('web_port', 5000)}",
        ]
        with open(self._env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _load_settings(self) -> dict:
        s = dict(self._SETTINGS_DEFAULTS)
        if os.path.exists(self._settings_path):
            try:
                with open(self._settings_path, encoding="utf-8") as f:
                    s.update(json.load(f))
            except Exception:
                pass
        return s

    def _save_settings(self) -> None:
        with self._config_lock:
            c = dict(self._config)
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
        }
        with open(self._settings_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _autosave(self) -> None:
        self._save_settings()
        self._save_env()
        t = threading.Timer(10, self._autosave)
        t.daemon = True
        t.start()
```

- [ ] **Step 7: Commit**

```bash
git add twitch_bot.py
git commit -m "feat: add WebApp class skeleton with config, logging, persistence"
```

---

## Task 4: Add service lifecycle, dispatch logic, and IRC/Discord connect methods

**Files:**
- Modify: `twitch_bot.py` (append to `WebApp`)

- [ ] **Step 1: Add service lifecycle methods**

```python
    # ══════════════════════════════════════════════════════════════════════════
    # Service lifecycle
    # ══════════════════════════════════════════════════════════════════════════

    def _start_services(self) -> None:
        self._tts = TTSEngine(get_config=self._get_tts_cfg, log=self._log)
        self._ai  = AIResponseHandler(
            get_config=self._get_ai_cfg,
            log=self._log,
            tts=self._tts,
        )

    def _stop_services(self) -> None:
        if self._discord:
            self._discord.disconnect()
            self._discord = None
        if self._tts:
            self._tts.stop()
            self._tts = None
        if self._ai:
            self._ai.stop()
            self._ai = None
```

- [ ] **Step 2: Add Twitch connect/disconnect methods**

```python
    # ══════════════════════════════════════════════════════════════════════════
    # Twitch IRC connect / disconnect
    # ══════════════════════════════════════════════════════════════════════════

    def _connect(self) -> None:
        self._save_env()
        with self._config_lock:
            self._config["twitch_status"] = "connecting"
        self._broadcast_status()
        self._irc = TwitchIRCClient(
            get_creds=self._get_irc_creds,
            log=self._log,
            on_message=self._dispatch,
        )
        self._irc.connect()
        self._log("[System] Connecting to Twitch IRC…")

    def _disconnect(self) -> None:
        irc = self._irc
        if irc:
            irc.disconnect()
            self._irc = None
        with self._config_lock:
            self._config["twitch_status"] = "off"
        self._broadcast_status()
        self._log("[System] Disconnected.")
```

- [ ] **Step 3: Add Discord connect/disconnect methods**

```python
    # ══════════════════════════════════════════════════════════════════════════
    # Discord connect / disconnect
    # ══════════════════════════════════════════════════════════════════════════

    def _discord_connect(self) -> None:
        if self._discord:
            self._discord.disconnect()
        self._save_env()

        def on_ready_cb() -> None:
            with self._config_lock:
                self._config["discord_status"] = "online"
            self._broadcast_status()

        def on_failure_cb() -> None:
            with self._config_lock:
                self._config["discord_status"] = "error"
            self._discord = None
            self._broadcast_status()

        with self._config_lock:
            self._config["discord_status"] = "connecting"
        self._broadcast_status()

        self._discord = DiscordClient(
            get_config=self._get_discord_cfg,
            log=self._log,
            ai_handler=self._ai,
            on_ready_cb=on_ready_cb,
            on_failure_cb=on_failure_cb,
        )
        self._discord.connect()
        self._log("[Discord] Connecting…")

    def _discord_disconnect(self) -> None:
        discord = self._discord
        if discord:
            discord.disconnect()
            self._discord = None
        with self._config_lock:
            self._config["discord_status"] = "off"
        self._broadcast_status()
        self._log("[Discord] Disconnected.")
```

- [ ] **Step 4: Add message dispatch methods**

```python
    # ══════════════════════════════════════════════════════════════════════════
    # Message dispatch  (called from IRC thread)
    # ══════════════════════════════════════════════════════════════════════════

    def _dispatch(self, username: str, message: str,
                  bits: int = 0, reward_id: str = "") -> None:
        tag = (f"  [{bits} bits]" if bits
               else (f"  [channel points]" if reward_id else ""))
        self._log(f"[Chat] {username}{tag}: {message}")
        if reward_id:
            self._log(f"[Chat] Reward ID: {reward_id}")
        self._route_plays(username, message)
        self._route_ai(username, message, bits, reward_id)
        # Mark Twitch as online on first message
        with self._config_lock:
            if self._config["twitch_status"] != "online":
                self._config["twitch_status"] = "online"
                do_broadcast = True
            else:
                do_broadcast = False
        if do_broadcast:
            self._broadcast_status()

    def _route_plays(self, username: str, message: str) -> None:
        with self._config_lock:
            enabled     = self._config.get("plays_enabled", False)
            command_map = dict(self._config.get("command_map", {}))
        word = message.strip().split()[0].lower() if message.strip() else ""
        if word not in command_map or not enabled:
            return
        entry = command_map[word]
        self._log(
            f"[Plays] {username} → {word}  "
            f"(key '{entry['key']}' × {entry['duration']}s)"
        )
        GameInputController(_BoolGetter(enabled)).execute(
            entry["key"], entry["duration"]
        )

    def _route_ai(self, username: str, message: str,
                  bits: int = 0, reward_id: str = "") -> None:
        with self._config_lock:
            ai_enabled       = self._config.get("ai_enabled",       False)
            trigger_every_n  = self._config.get("trigger_every_n",  True)
            every_n          = self._config.get("every_n",          5)
            trig_mentions    = self._config.get("trigger_mentions",  False)
            trig_bits        = self._config.get("trigger_bits",      False)
            min_bits         = self._config.get("min_bits",          100)
            trig_points      = self._config.get("trigger_points",    False)
            required_reward  = self._config.get("reward_id",         "")
            bot_user         = self._config.get("twitch_username",   "").lower()

        if not ai_enabled:
            return

        triggered = False

        if trig_mentions and bot_user and bot_user in message.lower():
            triggered = True

        if trig_bits and bits > 0 and bits >= max(1, min_bits):
            triggered = True
            self._log(f"[AI] Bits trigger: {username} cheered {bits} bits")

        if trig_points and reward_id:
            if not required_reward or reward_id.lower() == required_reward.lower():
                triggered = True
                self._log(f"[AI] Points trigger: {username} redeemed (ID: {reward_id})")
            else:
                self._log(f"[AI] Unmatched redemption — reward ID: {reward_id}")

        if trigger_every_n:
            self._ai_counter += 1
            if self._ai_counter >= max(1, every_n):
                self._ai_counter = 0
                triggered = True

        ai = self._ai
        if triggered and ai:
            ai.handle(username, message)
```

- [ ] **Step 5: Commit**

```bash
git add twitch_bot.py
git commit -m "feat: add WebApp service lifecycle and message dispatch"
```

---

## Task 5: Add Flask routes — core (/, /stream, /api/state) and lifecycle

**Files:**
- Modify: `twitch_bot.py` (append `_register_routes` method to `WebApp`)

- [ ] **Step 1: Add `_register_routes` with core routes**

```python
    # ══════════════════════════════════════════════════════════════════════════
    # Flask routes
    # ══════════════════════════════════════════════════════════════════════════

    def _register_routes(self) -> None:  # noqa: C901
        app = self._flask

        # ── page ──────────────────────────────────────────────────────────────

        @app.route("/")
        def index():
            return _flask.render_template("index.html")

        # ── SSE log stream ────────────────────────────────────────────────────

        @app.route("/stream")
        def stream():
            q: queue.Queue = queue.Queue()
            # Register client and send history before entering the loop
            with self._log_lock:
                history = list(self._log_ring)
                self._sse_clients.append(q)
            for line in history:
                q.put(f"data: {line}\n\n")

            def generate():
                try:
                    while True:
                        try:
                            yield q.get(timeout=30)
                        except queue.Empty:
                            yield ": keepalive\n\n"
                except GeneratorExit:
                    pass
                finally:
                    with self._log_lock:
                        try:
                            self._sse_clients.remove(q)
                        except ValueError:
                            pass

            return _flask.Response(
                generate(),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # ── state snapshot ────────────────────────────────────────────────────

        @app.route("/api/state")
        def api_state():
            with self._config_lock:
                c = dict(self._config)
            with self._log_lock:
                history = list(self._log_ring)
            return _flask.jsonify({
                "twitch_status":    c["twitch_status"],
                "discord_status":   c["discord_status"],
                "ai_enabled":       c["ai_enabled"],
                "plays_enabled":    c["plays_enabled"],
                "trigger_every_n":  c["trigger_every_n"],
                "every_n":          c["every_n"],
                "trigger_mentions": c["trigger_mentions"],
                "trigger_bits":     c["trigger_bits"],
                "min_bits":         c["min_bits"],
                "trigger_points":   c["trigger_points"],
                "reward_id":        c["reward_id"],
                "tts_ai":           c["tts_ai"],
                "command_map":      c["command_map"],
                "system_prompt":    c["system_prompt"],
                "last_prompt":      c["last_prompt"],
                "log_history":      history,
                "providers":        list(_PROVIDERS.keys()),
                "provider_endpoints": {k: v["endpoint"] for k, v in _PROVIDERS.items()},
                "provider_needs_key": {k: v["needs_key"] for k, v in _PROVIDERS.items()},
            })

        # ── Twitch lifecycle ──────────────────────────────────────────────────

        @app.route("/api/connect", methods=["POST"])
        def api_connect():
            # Merge any posted settings before connecting
            data = _flask.request.get_json(force=True, silent=True) or {}
            if data:
                with self._config_lock:
                    for k in ("twitch_channel", "twitch_username",
                               "twitch_client_id", "twitch_token"):
                        if k in data:
                            self._config[k] = data[k]
            self._connect()
            return _flask.jsonify({"ok": True})

        @app.route("/api/disconnect", methods=["POST"])
        def api_disconnect():
            self._disconnect()
            return _flask.jsonify({"ok": True})

        # ── Discord lifecycle ─────────────────────────────────────────────────

        @app.route("/api/discord/connect", methods=["POST"])
        def api_discord_connect():
            data = _flask.request.get_json(force=True, silent=True) or {}
            if data:
                with self._config_lock:
                    for k in ("discord_token", "discord_channel_id",
                               "discord_trigger", "discord_use_shared_prompt",
                               "discord_prompt"):
                        if k in data:
                            self._config[k] = data[k]
            self._discord_connect()
            return _flask.jsonify({"ok": True})

        @app.route("/api/discord/disconnect", methods=["POST"])
        def api_discord_disconnect():
            self._discord_disconnect()
            return _flask.jsonify({"ok": True})
```

- [ ] **Step 2: Add control and settings routes (still inside `_register_routes`)**

```python
        # ── controls ─────────────────────────────────────────────────────────

        @app.route("/api/ai/toggle", methods=["POST"])
        def api_ai_toggle():
            with self._config_lock:
                self._config["ai_enabled"] = not self._config["ai_enabled"]
                val = self._config["ai_enabled"]
            return _flask.jsonify({"ai_enabled": val})

        @app.route("/api/input/toggle", methods=["POST"])
        def api_input_toggle():
            with self._config_lock:
                self._config["plays_enabled"] = not self._config["plays_enabled"]
                val = self._config["plays_enabled"]
            return _flask.jsonify({"plays_enabled": val})

        @app.route("/api/tts/panic", methods=["POST"])
        def api_tts_panic():
            tts = self._tts
            if tts:
                tts.panic()
            self._log("[TTS] Panic — audio stopped and queue cleared.")
            return _flask.jsonify({"ok": True})

        @app.route("/api/commands", methods=["POST"])
        def api_commands():
            data = _flask.request.get_json(force=True, silent=True) or {}
            commands = {k: v for k, v in data.get("commands", {}).items()}
            with self._config_lock:
                self._config["command_map"] = commands
            return _flask.jsonify({"ok": True})

        @app.route("/api/ai/manual", methods=["POST"])
        def api_ai_manual():
            data = _flask.request.get_json(force=True, silent=True) or {}
            msg = (data.get("message") or "").strip()
            if not msg:
                return _flask.jsonify({"error": "empty message"}), 400
            self._log(f"[Host]: {msg}")
            ai = self._ai
            if not ai:
                return _flask.jsonify({"error": "AI not initialised"}), 503
            ai.handle("Host", msg)
            return _flask.jsonify({"ok": True})

        # ── settings ─────────────────────────────────────────────────────────

        _SETTINGS_KEYS = (
            "twitch_channel", "twitch_username", "twitch_client_id", "twitch_token",
            "llm_provider", "llm_endpoint", "llm_model", "llm_api_key",
            "piper_exe", "piper_model", "piper_config",
            "discord_token", "discord_channel_id", "discord_trigger",
            "discord_use_shared_prompt", "discord_prompt",
            "trigger_every_n", "every_n", "trigger_mentions", "trigger_bits",
            "min_bits", "trigger_points", "reward_id", "tts_ai",
        )

        @app.route("/api/settings", methods=["GET"])
        def api_settings_get():
            with self._config_lock:
                c = dict(self._config)
            return _flask.jsonify({k: c.get(k) for k in _SETTINGS_KEYS})

        @app.route("/api/settings", methods=["POST"])
        def api_settings_post():
            data = _flask.request.get_json(force=True, silent=True) or {}
            with self._config_lock:
                for k in _SETTINGS_KEYS:
                    if k in data:
                        self._config[k] = data[k]
                if "system_prompt" in data:
                    self._config["system_prompt"] = data["system_prompt"]
            self._save_env()
            self._log("[System] Settings saved.")
            return _flask.jsonify({"ok": True})
```

- [ ] **Step 3: Add model, prompt, and file-browser routes (still inside `_register_routes`)**

```python
        # ── models ───────────────────────────────────────────────────────────

        @app.route("/api/models")
        def api_models():
            import urllib.parse
            provider = _flask.request.args.get("provider", "Ollama")
            with self._config_lock:
                api_key  = self._config.get("llm_api_key", "")
                endpoint = self._config.get("llm_endpoint", "")

            if provider == "Claude":
                return _flask.jsonify({"models": _CLAUDE_MODELS})

            parsed = urllib.parse.urlparse(endpoint)
            base   = f"{parsed.scheme}://{parsed.netloc}"
            models: list[str] = []

            if provider == "Gemini":
                try:
                    url  = ("https://generativelanguage.googleapis.com"
                            f"/v1beta/models?key={api_key}")
                    resp = requests.get(url, timeout=10)
                    resp.raise_for_status()
                    models = [
                        m["name"].removeprefix("models/")
                        for m in resp.json().get("models", [])
                        if "generateContent" in m.get("supportedGenerationMethods", [])
                    ]
                except Exception:
                    pass
            elif provider in ("OpenAI", "Grok"):
                try:
                    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                    resp    = requests.get(f"{base}/v1/models",
                                           headers=headers, timeout=10)
                    resp.raise_for_status()
                    models  = sorted(i["id"] for i in resp.json().get("data", []))
                except Exception:
                    pass
            else:
                for url, key, sub in [
                    (f"{base}/v1/models", "data",   "id"),
                    (f"{base}/api/tags",  "models", "name"),
                ]:
                    try:
                        resp   = requests.get(url, timeout=5)
                        resp.raise_for_status()
                        models = [i[sub] for i in resp.json().get(key, []) if sub in i]
                        if models:
                            break
                    except Exception:
                        continue

            return _flask.jsonify({"models": models})

        # ── prompts ───────────────────────────────────────────────────────────

        @app.route("/api/prompts")
        def api_prompts_list():
            names: list[str] = []
            if os.path.isdir(self._prompts_dir):
                names = sorted(
                    f[:-4] for f in os.listdir(self._prompts_dir)
                    if f.endswith(".txt")
                )
            return _flask.jsonify({"prompts": names})

        @app.route("/api/prompts/<name>", methods=["GET"])
        def api_prompt_load(name: str):
            safe = re.sub(r"[^\w\s\-]", "", name).strip()
            if not safe:
                return _flask.jsonify({"error": "invalid name"}), 400
            path = os.path.join(self._prompts_dir, f"{safe}.txt")
            if not os.path.exists(path):
                return _flask.jsonify({"error": "not found"}), 404
            with open(path, encoding="utf-8") as f:
                content = f.read()
            return _flask.jsonify({"name": safe, "content": content})

        @app.route("/api/prompts/<name>", methods=["POST"])
        def api_prompt_save(name: str):
            safe = re.sub(r"[^\w\s\-]", "", name).strip()
            if not safe:
                return _flask.jsonify({"error": "invalid name"}), 400
            data    = _flask.request.get_json(force=True, silent=True) or {}
            content = data.get("content", "")
            os.makedirs(self._prompts_dir, exist_ok=True)
            with open(os.path.join(self._prompts_dir, f"{safe}.txt"),
                      "w", encoding="utf-8") as f:
                f.write(content)
            with self._config_lock:
                self._config["system_prompt"] = content
                self._config["last_prompt"]   = safe
            self._log(f"[Prompts] Saved → {safe}")
            return _flask.jsonify({"ok": True, "name": safe})

        # ── presets ───────────────────────────────────────────────────────────

        @app.route("/api/presets")
        def api_presets_list():
            names: list[str] = []
            if os.path.isdir(self._presets_dir):
                names = sorted(
                    f[:-5] for f in os.listdir(self._presets_dir)
                    if f.endswith(".json")
                )
            return _flask.jsonify({"presets": names})

        @app.route("/api/presets/<name>", methods=["GET"])
        def api_preset_load(name: str):
            safe = re.sub(r"[^\w\s\-]", "", name).strip()
            if not safe:
                return _flask.jsonify({"error": "invalid name"}), 400
            path = os.path.join(self._presets_dir, f"{safe}.json")
            if not os.path.exists(path):
                return _flask.jsonify({"error": "not found"}), 404
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return _flask.jsonify({"name": safe, "commands": data})

        @app.route("/api/presets/<name>", methods=["POST"])
        def api_preset_save(name: str):
            safe = re.sub(r"[^\w\s\-]", "", name).strip()
            if not safe:
                return _flask.jsonify({"error": "invalid name"}), 400
            with self._config_lock:
                commands = dict(self._config.get("command_map", {}))
            os.makedirs(self._presets_dir, exist_ok=True)
            with open(os.path.join(self._presets_dir, f"{safe}.json"),
                      "w", encoding="utf-8") as f:
                json.dump(commands, f, indent=2)
            self._log(f"[Presets] Saved → {safe}")
            return _flask.jsonify({"ok": True, "name": safe})

        # ── file browser ──────────────────────────────────────────────────────

        @app.route("/api/browse")
        def api_browse():
            requested = _flask.request.args.get("path", self._here)
            root      = os.path.realpath(requested)
            if not os.path.isdir(root):
                root = self._here
            try:
                dirs  = sorted(
                    e.name for e in os.scandir(root)
                    if e.is_dir() and not e.name.startswith(".")
                )
            except PermissionError:
                dirs = []
            try:
                files = sorted(
                    e.name for e in os.scandir(root)
                    if e.is_file() and not e.name.startswith(".")
                )
            except PermissionError:
                files = []
            parent = str(os.path.dirname(root)) if root != os.path.dirname(root) else None
            return _flask.jsonify({
                "path":   root,
                "parent": parent,
                "dirs":   dirs,
                "files":  files,
            })
```

- [ ] **Step 4: Commit**

```bash
git add twitch_bot.py
git commit -m "feat: add all Flask API routes to WebApp"
```

---

## Task 6: Add `__main__` entry point

**Files:**
- Modify: `twitch_bot.py` (append after `WebApp` class)

- [ ] **Step 1: Add entry point**

Append at the very end of `twitch_bot.py`:

```python
# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    web = WebApp()
    port = web._config.get("web_port", 5000)
    print(f"[System] Web UI starting → http://0.0.0.0:{port}")
    print(f"[System] Open http://<your-ip>:{port} in your browser")
    web._flask.run(host="0.0.0.0", port=port, threaded=True, debug=False)
```

- [ ] **Step 2: Verify the file parses cleanly**

```bash
.venv/bin/python -c "import twitch_bot; print('OK')"
```

Expected output: `OK` (no import errors). Note: this import will try to start services — if TTS/AI fail due to missing config, those are expected log messages. The test just checks for no syntax errors.

- [ ] **Step 3: Start the server to verify basic operation**

```bash
.venv/bin/python twitch_bot.py
```

Expected: prints the startup lines and starts without crashing. Flask should begin listening on port 5000. Press Ctrl+C to stop.

- [ ] **Step 4: Commit**

```bash
git add twitch_bot.py
git commit -m "feat: add Flask entry point with WEB_PORT support"
```

---

## Task 7: Update existing tests

**Files:**
- Modify: `tests/test_discord_integration.py`

The existing tests mock `customtkinter` since importing `twitch_bot` used to import it. After the migration, that mock is no longer needed.

- [ ] **Step 1: Remove the customtkinter mock from the test file**

In `tests/test_discord_integration.py`, find every occurrence of this pattern:

```python
with patch.dict("sys.modules", {"customtkinter": MagicMock()}):
    import importlib
    import twitch_bot
    importlib.reload(twitch_bot)
```

Replace each with:

```python
import twitch_bot
```

There are two occurrences: one in `TestAIHandlerReplyCallback._make_handler` and one in `TestDiscordClientTriggerMode._is_triggered`.

- [ ] **Step 2: Run the existing tests to confirm they still pass**

```bash
.venv/bin/python -m pytest tests/test_discord_integration.py -v
```

Expected: all 9 tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_discord_integration.py
git commit -m "test: remove customtkinter mock (no longer a dependency)"
```

---

## Task 8: Create `templates/index.html` — full single-page web UI

**Files:**
- Create: `templates/index.html`

- [ ] **Step 1: Create the `templates/` directory**

```bash
mkdir -p templates
```

- [ ] **Step 2: Write the complete `templates/index.html`**

Create the file with this content:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Twitch Interactive Bot</title>
<style>
:root{
  --bg:#111318;--surface:#1a1c24;--card:#1e2130;--border:#2d3148;
  --accent:#3daee9;--green:#1a7f37;--green-h:#15662d;
  --red:#c0392b;--red-h:#922b21;--orange:#f39c12;
  --text:#dde1f0;--muted:#7a7f9a;--on:#2ecc71;--off:#e74c3c;
  --font:system-ui,-apple-system,sans-serif;--mono:'Courier New',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px;height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* ── header ── */
#header{background:var(--surface);border-bottom:1px solid var(--border);padding:0 12px;height:48px;display:flex;align-items:center;gap:8px;flex-shrink:0}
#header h1{font-size:14px;font-weight:700;white-space:nowrap;margin-right:auto}
.pill{font-size:12px;padding:2px 10px;border-radius:12px;white-space:nowrap}
.pill.off{color:var(--off)}.pill.connecting{color:var(--orange)}.pill.online{color:var(--on)}.pill.error{color:var(--off)}

/* ── buttons ── */
.btn{border:none;border-radius:6px;padding:5px 12px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap;transition:opacity .15s}
.btn:disabled{opacity:.4;cursor:default}
.btn-green{background:var(--green);color:#fff}.btn-green:hover:not(:disabled){background:var(--green-h)}
.btn-red{background:var(--red);color:#fff}.btn-red:hover:not(:disabled){background:var(--red-h)}
.btn-panic{background:#8b0000;color:#fff}.btn-panic:hover:not(:disabled){background:#b22222}
.btn-neutral{background:var(--card);color:var(--text);border:1px solid var(--border)}.btn-neutral:hover:not(:disabled){background:var(--border)}
.btn-sm{padding:3px 9px;font-size:12px}
.btn-icon{background:transparent;border:1px solid var(--border);border-radius:6px;padding:4px 8px;cursor:pointer;color:var(--muted)}
.btn-icon:hover{color:var(--text)}

/* ── main layout ── */
#main{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:8px;flex:1;min-height:0;overflow:hidden}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;display:flex;flex-direction:column;overflow:hidden}

/* ── panel header (toggle rows) ── */
.panel-hdr{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--border);flex-shrink:0}
.panel-hdr span{font-weight:700;font-size:13px}
.status-badge{font-size:12px;font-weight:700}.status-badge.on{color:var(--on)}.status-badge.off{color:var(--off)}

/* ── toggle switch ── */
.toggle{position:relative;display:inline-block;width:40px;height:22px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:#444;border-radius:22px;cursor:pointer;transition:.2s}
.slider:before{content:"";position:absolute;width:16px;height:16px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s}
input:checked+.slider{background:var(--green)}
input:checked+.slider:before{transform:translateX(18px)}

/* ── form controls ── */
input[type=text],input[type=password],input[type=number],select,textarea{
  background:var(--surface);border:1px solid var(--border);border-radius:5px;
  color:var(--text);font-family:inherit;font-size:13px;padding:5px 8px;
  outline:none;width:100%;
}
input[type=text]:focus,input[type=password]:focus,input[type=number]:focus,select:focus,textarea:focus{border-color:var(--accent)}
input[type=number]{width:70px}
textarea{resize:vertical;font-family:var(--mono)}
.field-row{display:flex;align-items:center;gap:8px;padding:5px 0}
.field-row label{min-width:130px;color:var(--muted);font-size:12px;text-align:right;flex-shrink:0}
.field-with-btn{display:flex;gap:6px;flex:1}
.field-with-btn input,.field-with-btn select{flex:1}
.row-check{display:flex;align-items:center;gap:8px;padding:4px 0;cursor:pointer}
.hint{color:var(--muted);font-size:11px;padding:2px 0 6px}
.warn{color:var(--orange);font-size:11px;padding:4px 0}
.section-lbl{font-size:11px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.06em;padding:8px 0 4px}
.divider{border-top:1px solid var(--border);margin:8px 0}

/* ── plays panel ── */
#plays-inner{flex:1;overflow-y:auto;padding:8px 14px}
.preset-bar{display:flex;align-items:center;gap:6px;padding:6px 14px;border-bottom:1px solid var(--border);flex-shrink:0}
.preset-bar select{flex:1}
.add-bar{display:flex;align-items:center;gap:6px;padding:6px 14px;border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap}
.add-bar input{flex:1;min-width:60px}
#add-dur{width:65px}
.cmd-row{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;margin-bottom:4px}
.cmd-row:nth-child(even){background:rgba(255,255,255,.03)}
.cmd-name{font-family:var(--mono);color:var(--accent);font-weight:700;min-width:90px}
.cmd-info{flex:1;color:var(--muted);font-size:12px}
#no-cmds{color:var(--muted);font-size:13px;text-align:center;padding:20px}

/* ── ai panel ── */
#ai-inner{flex:1;display:flex;flex-direction:column;overflow:hidden}
#trig-box{padding:8px 14px;border-bottom:1px solid var(--border);flex-shrink:0}
#prompt-hdr{display:flex;align-items:center;gap:8px;padding:6px 14px;border-bottom:1px solid var(--border);flex-shrink:0}
#prompt-hdr .section-lbl{margin:0;flex:1}
#prompt-hdr select{width:160px}
#system-prompt{flex:1;resize:none;border:none;border-radius:0;background:var(--surface);padding:10px 14px;font-size:13px;font-family:var(--mono)}

/* ── console ── */
#console-bar{display:flex;align-items:center;gap:8px;padding:4px 12px;background:var(--surface);border-top:1px solid var(--border);border-bottom:1px solid var(--border);flex-shrink:0}
#console-bar span{font-size:11px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.06em;flex:1}
#console{flex:0 0 160px;overflow-y:auto;background:#0a0c10;padding:8px 12px;font-family:var(--mono);font-size:12px;color:#c0c8d8;line-height:1.5;flex-shrink:0}

/* ── manual prompt bar ── */
#manual-bar{display:flex;gap:6px;padding:6px 12px;background:var(--surface);border-top:1px solid var(--border);flex-shrink:0}
#manual-input{flex:1}

/* ── modal backdrop + box ── */
.modal{display:none;position:fixed;inset:0;z-index:100}
.modal.open{display:flex;align-items:center;justify-content:center}
.modal-bg{position:absolute;inset:0;background:rgba(0,0,0,.6)}
.modal-box{position:relative;background:var(--surface);border:1px solid var(--border);border-radius:10px;width:560px;max-height:80vh;display:flex;flex-direction:column;z-index:1;overflow:hidden}
.modal-hdr{display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border);font-weight:700;font-size:14px}
.modal-hdr .close{margin-left:auto;background:none;border:none;color:var(--muted);cursor:pointer;font-size:18px;line-height:1;padding:0 4px}
.modal-hdr .close:hover{color:var(--text)}
.modal-body{overflow-y:auto;padding:12px 16px;flex:1}
.modal-ftr{padding:10px 16px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px}

/* ── settings tabs ── */
.tab-bar{display:flex;border-bottom:1px solid var(--border);padding:0 16px}
.tab{background:none;border:none;padding:8px 14px;cursor:pointer;color:var(--muted);font-size:13px;border-bottom:2px solid transparent;margin-bottom:-1px}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-pane{display:none}.tab-pane.active{display:block}

/* ── file browser ── */
.file-modal-box{width:480px}
#fb-path{font-size:12px;color:var(--muted);padding:4px 0 8px;word-break:break-all}
.fb-entry{display:flex;align-items:center;gap:6px;padding:5px 4px;border-radius:4px;cursor:pointer;font-size:13px}
.fb-entry:hover{background:var(--border)}
.fb-icon{font-size:14px;width:20px;text-align:center;flex-shrink:0}
</style>
</head>
<body>

<!-- ════════════════════════════════ HEADER ════════════════════════════════ -->
<header id="header">
  <h1>Twitch Interactive Bot</h1>
  <button class="btn btn-panic" onclick="api('/api/tts/panic','POST')">⏹ Panic</button>
  <button id="btn-connect"    class="btn btn-green"   onclick="connectTwitch()">Connect</button>
  <button id="btn-disconnect" class="btn btn-red"     onclick="api('/api/disconnect','POST').then(loadState)" disabled>Disconnect</button>
  <span   id="twitch-status"  class="pill off">Twitch: ● Off</span>
  <button id="btn-dc-connect"    class="btn btn-green"   onclick="connectDiscord()">Discord</button>
  <button id="btn-dc-disconnect" class="btn btn-red"     onclick="api('/api/discord/disconnect','POST').then(loadState)" disabled>Discord Off</button>
  <span   id="discord-status"    class="pill off">Discord: ● Off</span>
  <button class="btn-icon" onclick="openSettings()" title="Settings">⚙</button>
</header>

<!-- ════════════════════════════════ MAIN ═════════════════════════════════ -->
<div id="main">

  <!-- ─── TWITCH PLAYS ─── -->
  <div class="card">
    <div class="panel-hdr">
      <span>Game Inputs Active</span>
      <label class="toggle" title="Toggle game input">
        <input type="checkbox" id="plays-toggle" onchange="togglePlays()">
        <span class="slider"></span>
      </label>
      <span id="plays-badge" class="status-badge off">OFF</span>
    </div>

    <!-- preset bar -->
    <div class="preset-bar">
      <label style="color:var(--muted);font-size:12px;white-space:nowrap">Preset:</label>
      <select id="preset-select" onchange="loadPreset(this.value)"></select>
      <button class="btn btn-neutral btn-sm" onclick="savePreset()">Save</button>
    </div>

    <!-- add command -->
    <div class="add-bar">
      <input id="add-cmd" type="text" placeholder="!jump" style="width:90px">
      <input id="add-key" type="text" placeholder="space" style="width:80px">
      <input id="add-dur" type="number" placeholder="0.5" min="0.01" step="0.1" style="width:65px">
      <button class="btn btn-green btn-sm" onclick="addCommand()">+ Add</button>
    </div>

    <div id="plays-inner">
      <div id="no-cmds">No mappings yet. Add one above.</div>
    </div>
  </div>

  <!-- ─── AI INTERACTION ─── -->
  <div class="card">
    <div class="panel-hdr">
      <span>AI Chat Reading Active</span>
      <label class="toggle" title="Toggle AI">
        <input type="checkbox" id="ai-toggle" onchange="toggleAI()">
        <span class="slider"></span>
      </label>
      <span id="ai-badge" class="status-badge off">OFF</span>
    </div>

    <div id="ai-inner">
      <div id="trig-box">
        <div class="section-lbl">Trigger Conditions</div>
        <label class="row-check"><input type="checkbox" id="trig-every-n" onchange="saveTriggers()"> Every <input type="number" id="every-n" value="5" min="1" style="width:55px;margin:0 4px" onchange="saveTriggers()"> messages</label>
        <label class="row-check"><input type="checkbox" id="trig-mentions" onchange="saveTriggers()"> @bot mentions</label>
        <label class="row-check"><input type="checkbox" id="trig-bits" onchange="saveTriggers()"> Bits cheer ≥ <input type="number" id="min-bits" value="100" min="1" style="width:65px;margin:0 4px" onchange="saveTriggers()"> bits</label>
        <label class="row-check"><input type="checkbox" id="trig-points" onchange="saveTriggers()"> Channel Point redeem</label>
        <div class="field-row" style="padding-left:24px">
          <label style="min-width:80px">Reward ID:</label>
          <input type="text" id="reward-id" placeholder="leave blank for any" onchange="saveTriggers()">
        </div>
        <div class="hint" style="padding-left:24px">Reward IDs appear in the Console when a redemption arrives.</div>
        <div class="divider"></div>
        <label class="row-check"><input type="checkbox" id="tts-ai" onchange="saveTriggers()"> Speak AI replies via TTS</label>
      </div>

      <div id="prompt-hdr">
        <span class="section-lbl">System Prompt</span>
        <select id="prompt-select" onchange="loadPrompt(this.value)"><option value="">— load —</option></select>
        <input id="prompt-name" type="text" placeholder="name" style="width:120px">
        <button class="btn btn-neutral btn-sm" onclick="savePrompt()">Save</button>
      </div>
      <textarea id="system-prompt" onchange="syncPrompt()" oninput="syncPrompt()"></textarea>
    </div>
  </div>
</div>

<!-- ════════════════════════════════ CONSOLE ══════════════════════════════ -->
<div id="console-bar">
  <span>Console</span>
  <button class="btn btn-neutral btn-sm" onclick="clearConsole()">Clear</button>
</div>
<pre id="console"></pre>

<!-- ════════════════════════ MANUAL AI PROMPT ════════════════════════════ -->
<div id="manual-bar">
  <input id="manual-input" type="text" placeholder="Message the AI…"
    onkeydown="if(event.key==='Enter')sendManual()">
  <button class="btn btn-neutral btn-sm" onclick="sendManual()">Send</button>
</div>

<!-- ════════════════════════════ SETTINGS MODAL ══════════════════════════ -->
<div id="settings-modal" class="modal">
  <div class="modal-bg" onclick="closeSettings()"></div>
  <div class="modal-box">
    <div class="modal-hdr">
      Connection Settings
      <button class="close" onclick="closeSettings()">✕</button>
    </div>
    <div class="tab-bar">
      <button class="tab active" onclick="showTab('twitch')">Twitch</button>
      <button class="tab"        onclick="showTab('ai')">AI</button>
      <button class="tab"        onclick="showTab('discord')">Discord</button>
      <button class="tab"        onclick="showTab('tts')">TTS</button>
    </div>
    <div class="modal-body">

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

      <!-- AI tab -->
      <div id="tab-ai" class="tab-pane">
        <div class="field-row"><label>Provider</label>
          <select id="s-provider" onchange="onProviderChange()"></select>
        </div>
        <div class="field-row" id="api-key-row"><label>API Key</label>
          <div class="field-with-btn">
            <input id="s-api-key" type="password" placeholder="sk-... / AIza... / xai-...">
            <button class="btn-icon btn-sm" onclick="toggleVis(this.previousElementSibling)">👁</button>
          </div>
        </div>
        <div class="field-row"><label>Endpoint</label><input id="s-endpoint" type="text" placeholder="http://localhost:11434/v1/chat/completions"></div>
        <div class="field-row"><label>Model</label>
          <div class="field-with-btn">
            <select id="s-model"></select>
            <button class="btn btn-neutral btn-sm" onclick="refreshModels()">Refresh</button>
          </div>
        </div>
      </div>

      <!-- Discord tab -->
      <div id="tab-discord" class="tab-pane">
        <div class="field-row"><label>Bot Token</label>
          <div class="field-with-btn">
            <input id="s-discord-token" type="password" placeholder="Bot xxxx...">
            <button class="btn-icon btn-sm" onclick="toggleVis(this.previousElementSibling)">👁</button>
          </div>
        </div>
        <div class="field-row"><label>Channel ID</label><input id="s-discord-channel" type="text" placeholder="123456789012345678"></div>
        <div class="field-row"><label>Trigger Mode</label>
          <select id="s-discord-trigger">
            <option>All messages</option>
            <option>@mention only</option>
            <option>@mention + replies</option>
            <option>All messages + mentions + replies</option>
          </select>
        </div>
        <div class="warn">⚠ Enable "Message Content Intent" in your bot's Discord Developer Portal.</div>
        <label class="row-check" style="margin:8px 0">
          <input type="checkbox" id="s-discord-shared" onchange="toggleDiscordPrompt()">
          Use shared Twitch system prompt
        </label>
        <textarea id="s-discord-prompt" rows="4" placeholder="Discord-specific system prompt…"></textarea>
      </div>

      <!-- TTS tab -->
      <div id="tab-tts" class="tab-pane">
        <div class="field-row"><label>Piper Executable</label>
          <div class="field-with-btn">
            <input id="s-piper-exe" type="text" placeholder="piper  or  /path/to/piper">
            <button class="btn btn-neutral btn-sm" onclick="openBrowser('s-piper-exe')">Browse</button>
          </div>
        </div>
        <div class="field-row"><label>Voice Model (.onnx)</label>
          <div class="field-with-btn">
            <input id="s-piper-model" type="text" placeholder="/path/to/voice.onnx">
            <button class="btn btn-neutral btn-sm" onclick="openBrowser('s-piper-model')">Browse</button>
          </div>
        </div>
        <div class="field-row"><label>Model Config (.json)</label>
          <div class="field-with-btn">
            <input id="s-piper-cfg" type="text" placeholder="/path/to/voice.onnx.json (optional)">
            <button class="btn btn-neutral btn-sm" onclick="openBrowser('s-piper-cfg')">Browse</button>
          </div>
        </div>
      </div>

    </div><!-- /modal-body -->
    <div class="modal-ftr">
      <button class="btn btn-neutral" onclick="closeSettings()">Cancel</button>
      <button class="btn btn-green"   onclick="saveSettings()">Save Settings</button>
    </div>
  </div>
</div>

<!-- ════════════════════════ FILE BROWSER MODAL ═══════════════════════════ -->
<div id="fb-modal" class="modal">
  <div class="modal-bg" onclick="closeBrowser()"></div>
  <div class="modal-box file-modal-box">
    <div class="modal-hdr">
      Browse Files
      <button class="close" onclick="closeBrowser()">✕</button>
    </div>
    <div class="modal-body">
      <div id="fb-path"></div>
      <div id="fb-list"></div>
    </div>
    <div class="modal-ftr">
      <button class="btn btn-neutral" onclick="closeBrowser()">Cancel</button>
    </div>
  </div>
</div>

<!-- ══════════════════════════════ JAVASCRIPT ═════════════════════════════ -->
<script>
'use strict';

// ── provider metadata (populated from /api/state) ─────────────────────────
let PROVIDERS = [];
let PROVIDER_ENDPOINTS = {};
let PROVIDER_NEEDS_KEY = {};

// ── file browser target field id ──────────────────────────────────────────
let _fbTarget = null;

// ── helpers ───────────────────────────────────────────────────────────────
function api(url, method='GET', body=null) {
  const opts = { method, headers: {'Content-Type':'application/json'} };
  if (body) opts.body = JSON.stringify(body);
  return fetch(url, opts).then(r => r.json());
}

function el(id) { return document.getElementById(id); }

function setStatusPill(id, status) {
  const e = el(id);
  e.className = 'pill ' + status;
  const labels = {off:'Off', connecting:'Connecting…', online:'On', error:'Error'};
  const prefix = id === 'twitch-status' ? 'Twitch' : 'Discord';
  e.textContent = `${prefix}: ● ${labels[status] || status}`;
}

function log(line) {
  const c = el('console');
  c.textContent += line + '\n';
  c.scrollTop = c.scrollHeight;
}

function clearConsole() { el('console').textContent = ''; }

function toggleVis(input) {
  input.type = input.type === 'password' ? 'text' : 'password';
}

// ── SSE stream ────────────────────────────────────────────────────────────
function startSSE() {
  const es = new EventSource('/stream');
  es.onmessage = e => log(e.data);
  es.addEventListener('status', e => {
    const s = JSON.parse(e.data);
    applyStatus(s.twitch_status, s.discord_status);
  });
  es.onerror = () => setTimeout(startSSE, 3000);
}

function applyStatus(twitch, discord) {
  setStatusPill('twitch-status', twitch);
  setStatusPill('discord-status', discord);
  el('btn-connect').disabled    = (twitch !== 'off');
  el('btn-disconnect').disabled = (twitch === 'off');
  el('btn-dc-connect').disabled    = (discord !== 'off');
  el('btn-dc-disconnect').disabled = (discord === 'off');
}

// ── state load ────────────────────────────────────────────────────────────
function loadState() {
  return api('/api/state').then(s => {
    // connection status
    applyStatus(s.twitch_status, s.discord_status);
    // provider metadata
    PROVIDERS = s.providers || [];
    PROVIDER_ENDPOINTS = s.provider_endpoints || {};
    PROVIDER_NEEDS_KEY = s.provider_needs_key || {};
    populateProviders();
    // toggles
    el('plays-toggle').checked = !!s.plays_enabled;
    el('plays-badge').textContent = s.plays_enabled ? 'ON' : 'OFF';
    el('plays-badge').className = 'status-badge ' + (s.plays_enabled ? 'on' : 'off');
    el('ai-toggle').checked = !!s.ai_enabled;
    el('ai-badge').textContent = s.ai_enabled ? 'ON' : 'OFF';
    el('ai-badge').className = 'status-badge ' + (s.ai_enabled ? 'on' : 'off');
    // trigger conditions
    el('trig-every-n').checked  = !!s.trigger_every_n;
    el('every-n').value         = s.every_n || 5;
    el('trig-mentions').checked = !!s.trigger_mentions;
    el('trig-bits').checked     = !!s.trigger_bits;
    el('min-bits').value        = s.min_bits || 100;
    el('trig-points').checked   = !!s.trigger_points;
    el('reward-id').value       = s.reward_id || '';
    el('tts-ai').checked        = (s.tts_ai !== false);
    // system prompt
    el('system-prompt').value   = s.system_prompt || '';
    el('prompt-name').value     = s.last_prompt || '';
    // command map
    renderCommands(s.command_map || {});
    // log history
    if (s.log_history && s.log_history.length) {
      const c = el('console');
      c.textContent = s.log_history.join('\n') + '\n';
      c.scrollTop = c.scrollHeight;
    }
    // prompts dropdown
    refreshPromptList();
    refreshPresetList();
    return s;
  });
}

// ── toggles ───────────────────────────────────────────────────────────────
function togglePlays() {
  api('/api/input/toggle','POST').then(r => {
    el('plays-badge').textContent = r.plays_enabled ? 'ON' : 'OFF';
    el('plays-badge').className = 'status-badge ' + (r.plays_enabled ? 'on' : 'off');
  });
}

function toggleAI() {
  api('/api/ai/toggle','POST').then(r => {
    el('ai-badge').textContent = r.ai_enabled ? 'ON' : 'OFF';
    el('ai-badge').className = 'status-badge ' + (r.ai_enabled ? 'on' : 'off');
  });
}

// ── save trigger settings (debounced) ────────────────────────────────────
let _trigTimer = null;
function saveTriggers() {
  clearTimeout(_trigTimer);
  _trigTimer = setTimeout(() => {
    api('/api/settings','POST', {
      trigger_every_n:  el('trig-every-n').checked,
      every_n:          parseInt(el('every-n').value) || 5,
      trigger_mentions: el('trig-mentions').checked,
      trigger_bits:     el('trig-bits').checked,
      min_bits:         parseInt(el('min-bits').value) || 100,
      trigger_points:   el('trig-points').checked,
      reward_id:        el('reward-id').value.trim(),
      tts_ai:           el('tts-ai').checked,
    });
  }, 600);
}

// ── system prompt sync ────────────────────────────────────────────────────
let _promptTimer = null;
function syncPrompt() {
  clearTimeout(_promptTimer);
  _promptTimer = setTimeout(() => {
    api('/api/settings','POST',{system_prompt: el('system-prompt').value});
  }, 800);
}

// ── connect / disconnect ──────────────────────────────────────────────────
function connectTwitch() {
  applyStatus('connecting', null);
  api('/api/connect','POST').catch(()=>applyStatus('error',null));
}

function connectDiscord() {
  applyStatus(null, 'connecting');
  api('/api/discord/connect','POST').catch(()=>applyStatus(null,'error'));
}

// ── manual AI message ─────────────────────────────────────────────────────
function sendManual() {
  const inp = el('manual-input');
  const msg = inp.value.trim();
  if (!msg) return;
  inp.value = '';
  api('/api/ai/manual','POST',{message: msg});
}

// ── command map ───────────────────────────────────────────────────────────
let _commandMap = {};

function renderCommands(map) {
  _commandMap = map;
  const box = el('plays-inner');
  box.innerHTML = '';
  const keys = Object.keys(map).sort();
  if (!keys.length) {
    box.innerHTML = '<div id="no-cmds">No mappings yet. Add one above.</div>';
    return;
  }
  keys.forEach(cmd => {
    const info = map[cmd];
    const row = document.createElement('div');
    row.className = 'cmd-row';
    row.innerHTML = `<span class="cmd-name">${cmd}</span>
      <span class="cmd-info">→ press <b>'${info.key}'</b> for ${info.duration}s</span>
      <button class="btn btn-red btn-sm" onclick="removeCommand('${cmd}')">Remove</button>`;
    box.appendChild(row);
  });
}

function addCommand() {
  let cmd = el('add-cmd').value.trim().toLowerCase();
  const key = el('add-key').value.trim().toLowerCase();
  const dur = parseFloat(el('add-dur').value) || 0.3;
  if (!cmd || !key) return;
  if (!cmd.startsWith('!')) cmd = '!' + cmd;
  _commandMap[cmd] = {key, duration: Math.round(dur * 1000) / 1000};
  el('add-cmd').value = '';
  el('add-key').value = '';
  el('add-dur').value = '';
  renderCommands(_commandMap);
  api('/api/commands','POST',{commands: _commandMap});
}

function removeCommand(cmd) {
  delete _commandMap[cmd];
  renderCommands(_commandMap);
  api('/api/commands','POST',{commands: _commandMap});
}

// ── presets ───────────────────────────────────────────────────────────────
function refreshPresetList() {
  api('/api/presets').then(r => {
    const sel = el('preset-select');
    const cur = sel.value;
    sel.innerHTML = '<option value="">— select —</option>';
    (r.presets || []).forEach(n => {
      const o = document.createElement('option');
      o.value = o.textContent = n;
      sel.appendChild(o);
    });
    if (cur) sel.value = cur;
  });
}

function loadPreset(name) {
  if (!name) return;
  api(`/api/presets/${encodeURIComponent(name)}`).then(r => {
    if (r.commands) {
      _commandMap = r.commands;
      renderCommands(_commandMap);
      api('/api/commands','POST',{commands: _commandMap});
    }
  });
}

function savePreset() {
  const sel = el('preset-select');
  let name = sel.value.trim();
  if (!name) {
    name = prompt('Preset name:');
    if (!name) return;
    name = name.trim();
  }
  api(`/api/presets/${encodeURIComponent(name)}`,'POST').then(() => {
    refreshPresetList();
    setTimeout(() => { el('preset-select').value = name; }, 200);
  });
}

// ── prompts ───────────────────────────────────────────────────────────────
function refreshPromptList() {
  api('/api/prompts').then(r => {
    const sel = el('prompt-select');
    sel.innerHTML = '<option value="">— load —</option>';
    (r.prompts || []).forEach(n => {
      const o = document.createElement('option');
      o.value = o.textContent = n;
      sel.appendChild(o);
    });
  });
}

function loadPrompt(name) {
  if (!name) return;
  api(`/api/prompts/${encodeURIComponent(name)}`).then(r => {
    if (r.content !== undefined) {
      el('system-prompt').value = r.content;
      el('prompt-name').value   = r.name;
      api('/api/settings','POST',{system_prompt: r.content, last_prompt: r.name});
    }
  });
  el('prompt-select').value = '';
}

function savePrompt() {
  const name    = el('prompt-name').value.trim();
  const content = el('system-prompt').value;
  if (!name) { alert('Enter a prompt name first.'); return; }
  api(`/api/prompts/${encodeURIComponent(name)}`,'POST',{content}).then(() => {
    refreshPromptList();
  });
}

// ── settings modal ────────────────────────────────────────────────────────
function openSettings() {
  // Pre-populate fields from current server state
  api('/api/settings').then(s => {
    el('s-channel').value           = s.twitch_channel    || '';
    el('s-username').value          = s.twitch_username   || '';
    el('s-client-id').value         = s.twitch_client_id  || '';
    el('s-token').value             = s.twitch_token      || '';
    el('s-api-key').value           = s.llm_api_key       || '';
    el('s-endpoint').value          = s.llm_endpoint      || '';
    el('s-discord-token').value     = s.discord_token     || '';
    el('s-discord-channel').value   = s.discord_channel_id|| '';
    el('s-discord-trigger').value   = s.discord_trigger   || 'All messages';
    el('s-discord-shared').checked  = (s.discord_use_shared_prompt !== false);
    el('s-discord-prompt').value    = s.discord_prompt    || '';
    el('s-piper-exe').value         = s.piper_exe         || '';
    el('s-piper-model').value       = s.piper_model       || '';
    el('s-piper-cfg').value         = s.piper_config      || '';
    toggleDiscordPrompt();
    // provider
    if (s.llm_provider) {
      el('s-provider').value = s.llm_provider;
      onProviderChange(false);
    }
    // populate model dropdown with current model
    const sel = el('s-model');
    sel.innerHTML = `<option value="${s.llm_model||''}">${s.llm_model||'—'}</option>`;
  });
  el('settings-modal').classList.add('open');
}

function closeSettings() { el('settings-modal').classList.remove('open'); }

function showTab(name) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  el('tab-' + name).classList.add('active');
  document.querySelectorAll('.tab').forEach(b => {
    if (b.textContent.toLowerCase().startsWith(name)) b.classList.add('active');
  });
}

function saveSettings() {
  const body = {
    twitch_channel:           el('s-channel').value.trim(),
    twitch_username:          el('s-username').value.trim(),
    twitch_client_id:         el('s-client-id').value.trim(),
    twitch_token:             el('s-token').value.trim(),
    llm_provider:             el('s-provider').value,
    llm_endpoint:             el('s-endpoint').value.trim(),
    llm_model:                el('s-model').value,
    llm_api_key:              el('s-api-key').value.trim(),
    piper_exe:                el('s-piper-exe').value.trim(),
    piper_model:              el('s-piper-model').value.trim(),
    piper_config:             el('s-piper-cfg').value.trim(),
    discord_token:            el('s-discord-token').value.trim(),
    discord_channel_id:       el('s-discord-channel').value.trim(),
    discord_trigger:          el('s-discord-trigger').value,
    discord_use_shared_prompt:el('s-discord-shared').checked,
    discord_prompt:           el('s-discord-prompt').value,
  };
  api('/api/settings','POST', body).then(() => closeSettings());
}

// ── provider helpers ──────────────────────────────────────────────────────
function populateProviders() {
  const sel = el('s-provider');
  const cur = sel.value;
  sel.innerHTML = '';
  PROVIDERS.forEach(p => {
    const o = document.createElement('option');
    o.value = o.textContent = p;
    sel.appendChild(o);
  });
  if (cur && PROVIDERS.includes(cur)) sel.value = cur;
}

function onProviderChange(autoFill=true) {
  const p = el('s-provider').value;
  const needsKey = PROVIDER_NEEDS_KEY[p] !== false;
  el('api-key-row').style.display = needsKey ? '' : 'none';
  if (autoFill && PROVIDER_ENDPOINTS[p]) {
    el('s-endpoint').value = PROVIDER_ENDPOINTS[p];
    refreshModels();
  }
}

function refreshModels() {
  const p = el('s-provider').value;
  api(`/api/models?provider=${encodeURIComponent(p)}`).then(r => {
    const sel = el('s-model');
    const cur = sel.value;
    sel.innerHTML = '';
    (r.models || []).forEach(m => {
      const o = document.createElement('option');
      o.value = o.textContent = m;
      sel.appendChild(o);
    });
    if (cur && r.models && r.models.includes(cur)) sel.value = cur;
  });
}

function getOAuthUrl() {
  const clientId = el('s-client-id').value.trim();
  if (!clientId) { alert('Enter your Client ID first.'); return; }
  const url = `https://id.twitch.tv/oauth2/authorize?client_id=${clientId}&redirect_uri=http://localhost&response_type=token&scope=chat:read+chat:edit`;
  log('[Auth] ── OAuth Authorization URL ──');
  log(url);
  log('[Auth] 1. Click Authorize on the Twitch page.');
  log('[Auth] 2. Browser redirects to localhost (error page is fine).');
  log('[Auth] 3. Copy the value between "access_token=" and "&scope" in the address bar.');
  log('[Auth] 4. Paste it into the OAuth Token field in Settings → Twitch.');
  log('[Auth] 5. Click Save Settings, then Connect.');
  window.open(url, '_blank');
  closeSettings();
}

function toggleDiscordPrompt() {
  el('s-discord-prompt').style.display =
    el('s-discord-shared').checked ? 'none' : '';
}

// ── file browser ──────────────────────────────────────────────────────────
function openBrowser(targetFieldId) {
  _fbTarget = targetFieldId;
  el('fb-modal').classList.add('open');
  browseTo(null);
}

function closeBrowser() {
  el('fb-modal').classList.remove('open');
  _fbTarget = null;
}

function browseTo(path) {
  const url = '/api/browse' + (path ? '?path=' + encodeURIComponent(path) : '');
  api(url).then(r => {
    el('fb-path').textContent = r.path;
    const list = el('fb-list');
    list.innerHTML = '';

    // Parent directory
    if (r.parent) {
      const row = document.createElement('div');
      row.className = 'fb-entry';
      row.innerHTML = '<span class="fb-icon">📁</span><span>..</span>';
      row.onclick = () => browseTo(r.parent);
      list.appendChild(row);
    }

    // Dirs
    r.dirs.forEach(d => {
      const row = document.createElement('div');
      row.className = 'fb-entry';
      row.innerHTML = `<span class="fb-icon">📁</span><span>${d}</span>`;
      row.onclick = () => browseTo(r.path + '/' + d);
      list.appendChild(row);
    });

    // Files
    r.files.forEach(f => {
      const row = document.createElement('div');
      row.className = 'fb-entry';
      row.innerHTML = `<span class="fb-icon">📄</span><span>${f}</span>`;
      row.onclick = () => {
        if (_fbTarget) el(_fbTarget).value = r.path + '/' + f;
        closeBrowser();
      };
      list.appendChild(row);
    });
  });
}

// ── applyStatus guard (allow null = no change) ────────────────────────────
const _origApplyStatus = applyStatus;
window.applyStatus = function(twitch, discord) {
  if (twitch === null) twitch = el('twitch-status').dataset.status;
  if (discord === null) discord = el('discord-status').dataset.status;
  el('twitch-status').dataset.status = twitch;
  el('discord-status').dataset.status = discord;
  _origApplyStatus(twitch, discord);
};

// ── init ──────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  loadState().then(() => startSSE());
});
</script>
</body>
</html>
```

- [ ] **Step 3: Start the server and verify in browser**

```bash
.venv/bin/python twitch_bot.py
```

Open `http://localhost:5000` in a browser. Verify:
- Header bar renders with Connect / Discord / Panic buttons
- Both panels appear side by side
- Console is visible at the bottom
- Settings gear icon opens the settings modal
- Browse buttons in TTS tab open the file browser modal

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "feat: add web UI frontend (Flask + SSE + vanilla JS)"
```

---

## Task 9: Update CLAUDE.md and push

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the Running section in CLAUDE.md**

Find the `## Running the project` section and replace with:

```markdown
## Running the project

```bash
# Start the web server (headless — no display required)
.venv/bin/python twitch_bot.py
```

Flask binds to `0.0.0.0` on port 5000 by default.
Access the UI at `http://<server-ip>:5000` from any device on the network.

Set `WEB_PORT=<port>` in `.env` to change the port.
```

- [ ] **Step 2: Update the Architecture section**

Replace the `TwitchBotApp` row in the table with:

```markdown
| `WebApp`             | Flask web server, service lifecycle, API routes, SSE log broadcast |
```

Add a note about the new threading model:

```markdown
**Additional threads:**
- SSE threads: one per connected browser tab (Flask `threaded=True`)
- Log ring buffer: `collections.deque(maxlen=200)` replaces GUI-poll pattern
```

- [ ] **Step 3: Update the .env additions section**

Add `WEB_PORT` to the list of `.env` key names.

- [ ] **Step 4: Commit and push**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for web GUI (Flask, headless, WEB_PORT)"
git push
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] Flask + SSE + vanilla JS — Task 8
- [x] `WEB_PORT` in `.env` — Task 3 (`_config["web_port"]`), Task 9 (docs)
- [x] Replace `customtkinter` — Tasks 2, 7
- [x] SSE log broadcast with ring buffer — Task 3 (`_log()`, `_log_ring`)
- [x] All API endpoints from spec — Task 5
- [x] File browser (`/api/browse`) — Task 5, Task 8 JS `browseTo()`
- [x] Settings modal with all tabs — Task 8
- [x] Twitch/Discord connect/disconnect — Tasks 4, 5, 8
- [x] AI toggles, trigger conditions — Tasks 5, 8
- [x] Preset save/load — Task 5, Task 8
- [x] Prompt save/load — Task 5, Task 8
- [x] Manual AI message bar — Task 8
- [x] Status SSE events — Task 3 (`_broadcast_status`), Task 8 (`es.addEventListener('status')`)
- [x] `GET /api/state` log history — Task 5
- [x] `_BoolGetter` for `GameInputController` — Task 2
- [x] TTS panic button — Tasks 5, 8
- [x] Auto-connect on startup — Task 3 (`threading.Timer`)
- [x] Autosave every 10s — Task 3 (`_autosave`)

**Type/name consistency:**
- `_commandMap` (JS) / `command_map` (Python dict key) — consistent
- `PROVIDER_ENDPOINTS` / `PROVIDER_NEEDS_KEY` (JS) match `provider_endpoints` / `provider_needs_key` from `/api/state`
- `_BoolGetter` referenced in Task 2 step 3 and used in Task 4 step 4 — consistent
