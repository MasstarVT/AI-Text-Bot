# Discord Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `DiscordClient` class to `twitch_bot.py` that connects to a Discord channel, routes messages through the existing `AIResponseHandler`, and posts AI replies back to Discord — independently of Twitch.

**Architecture:** A new `DiscordClient` runs `discord.py` on a daemon thread with its own asyncio event loop, filtering messages by configurable trigger mode, then calling the shared `AIResponseHandler.handle()` with a `reply_cb` that posts the AI reply back to Discord via `run_coroutine_threadsafe`. `AIResponseHandler` gains optional `reply_cb` and `prompt_override` params; all existing Twitch callers are unaffected. The UI adds a Discord section to the Connection Settings window plus a status label in the header.

**Tech Stack:** Python 3, discord.py ≥ 2.3.0, asyncio, threading, customtkinter, existing requests-based AIResponseHandler

---

## File Map

| File | Action | What changes |
|---|---|---|
| `requirements.txt` | Modify | Add `discord.py>=2.3.0` |
| `twitch_bot.py` | Modify | New `DiscordClient` class; extend `AIResponseHandler`; new UI section; new service lifecycle methods |
| `tests/test_discord_integration.py` | Create | Unit tests for `reply_cb`/`prompt_override` plumbing and trigger-mode filtering |

---

## Task 1: Add discord.py dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add discord.py to requirements.txt**

Open `requirements.txt` and append after the last line:

```
# Discord bot integration
discord.py>=2.3.0
```

- [ ] **Step 2: Install the dependency**

```bash
.venv/bin/pip install "discord.py>=2.3.0"
```

Expected output: `Successfully installed discord.py-2.x.x aiohttp-...`

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "feat: add discord.py dependency for Discord bot integration"
```

---

## Task 2: Write failing tests for AIResponseHandler reply_cb + prompt_override

**Files:**
- Create: `tests/test_discord_integration.py`

- [ ] **Step 1: Create the test file**

```bash
mkdir -p tests
```

Create `tests/test_discord_integration.py`:

```python
"""Unit tests for Discord integration — AIResponseHandler reply_cb and prompt_override."""
import queue
import threading
import unittest
from unittest.mock import MagicMock, patch


class FakeTTS:
    def speak(self, text): pass


class TestAIHandlerReplyCallback(unittest.TestCase):
    def _make_handler(self, fake_reply="Hello from AI"):
        """Build an AIResponseHandler with requests.post mocked to return fake_reply."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

        # Patch customtkinter before importing twitch_bot (it needs a display)
        with patch.dict("sys.modules", {"customtkinter": MagicMock()}):
            import importlib
            import twitch_bot
            importlib.reload(twitch_bot)

        cfg = {
            "provider": "Ollama",
            "endpoint": "http://localhost:11434/v1/chat/completions",
            "model": "llama3",
            "api_key": "",
            "system_prompt": "You are a test bot.",
            "tts_ai": False,
        }

        handler = twitch_bot.AIResponseHandler(
            get_config=lambda: cfg,
            log=lambda msg: None,
            tts=FakeTTS(),
        )

        # Patch out the HTTP call
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": fake_reply}, "finish_reason": "stop"}]
        }
        handler._mock_patch = patch("requests.post", return_value=mock_resp)
        handler._mock_patch.start()
        return handler

    def tearDown(self):
        pass  # patches are stopped per-handler

    def test_reply_cb_called_with_ai_response(self):
        """reply_cb receives the AI reply text."""
        handler = self._make_handler("Hello from AI")
        received = []
        done = threading.Event()

        def cb(text):
            received.append(text)
            done.set()

        handler.handle("testuser", "hi there", reply_cb=cb)
        done.wait(timeout=5)
        handler._mock_patch.stop()

        self.assertEqual(received, ["Hello from AI"])

    def test_reply_cb_none_does_not_crash(self):
        """Passing no reply_cb (Twitch path) still works."""
        handler = self._make_handler("AI says hi")
        done = threading.Event()

        original_query = handler._query

        def wrapped(*args, **kwargs):
            try:
                original_query(*args, **kwargs)
            finally:
                done.set()

        handler._query = wrapped
        handler.handle("twitchuser", "hello")
        done.wait(timeout=5)
        handler._mock_patch.stop()

    def test_prompt_override_used_when_provided(self):
        """prompt_override replaces the system_prompt from get_config."""
        import requests as req

        handler = self._make_handler("OK")
        captured_payload = []

        def fake_post(url, headers=None, json=None, timeout=None):
            captured_payload.append(json)
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]
            }
            return mock_resp

        handler._mock_patch.stop()
        done = threading.Event()

        with patch("requests.post", side_effect=fake_post):
            original_query = handler._query

            def wrapped(*args, **kwargs):
                try:
                    original_query(*args, **kwargs)
                finally:
                    done.set()

            handler._query = wrapped
            handler.handle("user", "msg", prompt_override="Discord-specific prompt")
            done.wait(timeout=5)

        self.assertTrue(len(captured_payload) > 0)
        messages = captured_payload[0]["messages"]
        system_msg = next((m for m in messages if m["role"] == "system"), None)
        self.assertIsNotNone(system_msg)
        self.assertEqual(system_msg["content"], "Discord-specific prompt")


class TestDiscordClientTriggerMode(unittest.TestCase):
    def _make_message(self, bot_user, mentions=None, is_reply_to_bot=False):
        msg = MagicMock()
        msg.author = MagicMock()
        msg.author.name = "testuser"
        msg.content = "hello"
        msg.mentions = mentions or []

        if is_reply_to_bot:
            msg.reference = MagicMock()
            msg.reference.resolved = MagicMock()
            msg.reference.resolved.author = bot_user
        else:
            msg.reference = None

        return msg

    def _is_triggered(self, trigger, bot_user, message):
        """Mirror of DiscordClient._is_triggered static method."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        with patch.dict("sys.modules", {"customtkinter": MagicMock()}):
            import importlib
            import twitch_bot
            importlib.reload(twitch_bot)
        return twitch_bot.DiscordClient._is_triggered(trigger, bot_user, message)

    def test_all_messages_always_triggers(self):
        bot = MagicMock()
        msg = self._make_message(bot)
        self.assertTrue(self._is_triggered("All messages", bot, msg))

    def test_all_plus_mentions_always_triggers(self):
        bot = MagicMock()
        msg = self._make_message(bot)
        self.assertTrue(self._is_triggered("All messages + mentions + replies", bot, msg))

    def test_mention_only_triggers_on_mention(self):
        bot = MagicMock()
        msg = self._make_message(bot, mentions=[bot])
        self.assertTrue(self._is_triggered("@mention only", bot, msg))

    def test_mention_only_no_trigger_without_mention(self):
        bot = MagicMock()
        msg = self._make_message(bot, mentions=[])
        self.assertFalse(self._is_triggered("@mention only", bot, msg))

    def test_mention_plus_replies_triggers_on_mention(self):
        bot = MagicMock()
        msg = self._make_message(bot, mentions=[bot])
        self.assertTrue(self._is_triggered("@mention + replies", bot, msg))

    def test_mention_plus_replies_triggers_on_reply(self):
        bot = MagicMock()
        msg = self._make_message(bot, mentions=[], is_reply_to_bot=True)
        self.assertTrue(self._is_triggered("@mention + replies", bot, msg))

    def test_mention_plus_replies_no_trigger_for_plain_message(self):
        bot = MagicMock()
        msg = self._make_message(bot, mentions=[], is_reply_to_bot=False)
        self.assertFalse(self._is_triggered("@mention + replies", bot, msg))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests — expect ImportError or AttributeError (AIResponseHandler missing reply_cb, DiscordClient doesn't exist yet)**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && .venv/bin/python -m pytest tests/test_discord_integration.py -v 2>&1 | head -40
```

Expected: tests fail with `TypeError` or `AttributeError` — confirms the tests are wired up correctly.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_discord_integration.py
git commit -m "test: add failing tests for Discord reply_cb, prompt_override, and trigger modes"
```

---

## Task 3: Extend AIResponseHandler to support reply_cb and prompt_override

**Files:**
- Modify: `twitch_bot.py:352-441` (AIResponseHandler class)

- [ ] **Step 1: Update the queue type annotation and handle() signature**

Find this block in `twitch_bot.py` (around line 356):
```python
    def __init__(self, get_config, log, tts: TTSEngine) -> None:
        self.get_config = get_config   # callable → dict(endpoint, model, system_prompt, tts_ai)
        self.log = log
        self.tts = tts
        self._q: queue.Queue[tuple[str, str] | None] = queue.Queue()
        threading.Thread(target=self._worker, name="AI-Worker", daemon=True).start()

    def handle(self, username: str, message: str) -> None:
        self._q.put((username, message))
```

Replace with:
```python
    def __init__(self, get_config, log, tts: TTSEngine) -> None:
        self.get_config = get_config   # callable → dict(endpoint, model, system_prompt, tts_ai)
        self.log = log
        self.tts = tts
        self._q: queue.Queue[tuple | None] = queue.Queue()
        threading.Thread(target=self._worker, name="AI-Worker", daemon=True).start()

    def handle(self, username: str, message: str, reply_cb=None, prompt_override: str | None = None) -> None:
        self._q.put((username, message, reply_cb, prompt_override))
```

- [ ] **Step 2: Update _worker() to unpack the new tuple shape**

Find (around line 367):
```python
    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            self._query(*item)
```

Replace with:
```python
    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            username, message, reply_cb, prompt_override = item
            self._query(username, message, reply_cb=reply_cb, prompt_override=prompt_override)
```

- [ ] **Step 3: Update _query() to accept and use reply_cb and prompt_override**

Find the `_query` signature and body (around line 374):
```python
    def _query(self, username: str, message: str) -> None:
        cfg           = self.get_config()
        provider      = cfg.get("provider", "Ollama")
        endpoint      = cfg.get("endpoint")      or "http://localhost:11434/v1/chat/completions"
        model         = cfg.get("model")         or "llama3"
        api_key       = cfg.get("api_key",  "")
        system_prompt = cfg.get("system_prompt") or "You are a helpful Twitch chat bot."
        use_tts       = cfg.get("tts_ai", True)
        fmt           = _PROVIDERS.get(provider, {}).get("fmt", "openai")

        try:
            if fmt == "anthropic":
                reply = self._query_anthropic(endpoint, model, api_key, system_prompt, username, message)
            else:
                reply = self._query_openai(endpoint, model, api_key, system_prompt, username, message)

            if not reply:
                self.log("[AI] Model returned an empty response.")
                return
            self.log(f"[AI] → {reply}")
            if use_tts:
                self.tts.speak(reply)
        except requests.exceptions.ConnectionError:
            self.log("[AI] Cannot reach AI server — check your endpoint and internet connection.")
        except requests.exceptions.Timeout:
            self.log("[AI] AI request timed out (>90 s).")
        except Exception as exc:
            self.log(f"[AI] Error: {exc}")
```

Replace with:
```python
    def _query(self, username: str, message: str, reply_cb=None, prompt_override: str | None = None) -> None:
        cfg           = self.get_config()
        provider      = cfg.get("provider", "Ollama")
        endpoint      = cfg.get("endpoint")      or "http://localhost:11434/v1/chat/completions"
        model         = cfg.get("model")         or "llama3"
        api_key       = cfg.get("api_key",  "")
        system_prompt = prompt_override or cfg.get("system_prompt") or "You are a helpful Twitch chat bot."
        use_tts       = cfg.get("tts_ai", True)
        fmt           = _PROVIDERS.get(provider, {}).get("fmt", "openai")

        try:
            if fmt == "anthropic":
                reply = self._query_anthropic(endpoint, model, api_key, system_prompt, username, message)
            else:
                reply = self._query_openai(endpoint, model, api_key, system_prompt, username, message)

            if not reply:
                self.log("[AI] Model returned an empty response.")
                return
            self.log(f"[AI] → {reply}")
            if use_tts:
                self.tts.speak(reply)
            if reply_cb is not None:
                try:
                    reply_cb(reply)
                except Exception as exc:
                    self.log(f"[AI] reply_cb error: {exc}")
        except requests.exceptions.ConnectionError:
            self.log("[AI] Cannot reach AI server — check your endpoint and internet connection.")
        except requests.exceptions.Timeout:
            self.log("[AI] AI request timed out (>90 s).")
        except Exception as exc:
            self.log(f"[AI] Error: {exc}")
```

- [ ] **Step 4: Run tests — AIResponseHandler tests should now pass**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && .venv/bin/python -m pytest tests/test_discord_integration.py::TestAIHandlerReplyCallback -v
```

Expected: 3 tests PASS. The `TestDiscordClientTriggerMode` tests still fail (DiscordClient doesn't exist yet).

- [ ] **Step 5: Commit**

```bash
git add twitch_bot.py
git commit -m "feat: extend AIResponseHandler with reply_cb and prompt_override support"
```

---

## Task 4: Add DiscordClient class

**Files:**
- Modify: `twitch_bot.py` — insert new class after `TwitchIRCClient` (after line ~562)

- [ ] **Step 1: Add the DiscordClient class**

Insert the following block in `twitch_bot.py` immediately after the closing line of `TwitchIRCClient` (the line containing `self.on_message(m.group(1), m.group(2).strip(), bits, reward_id)`) and before the `# ══ TwitchBotApp` banner:

```python

# ══════════════════════════════════════════════════════════════════════════════
# DiscordClient
# ══════════════════════════════════════════════════════════════════════════════
class DiscordClient:
    """
    Discord bot integration using discord.py.

    Runs a discord.Client on a dedicated daemon thread with its own asyncio
    event loop.  Filters incoming messages by trigger mode, then routes them
    through the shared AIResponseHandler with a reply_cb that posts the AI
    reply back to the originating Discord channel.
    """

    TRIGGER_MODES = [
        "All messages",
        "@mention only",
        "@mention + replies",
        "All messages + mentions + replies",
    ]

    def __init__(self, get_config, log, ai_handler: "AIResponseHandler") -> None:
        self.get_config = get_config  # callable → dict(discord_token, discord_channel_id, discord_trigger, discord_prompt)
        self.log = log
        self._ai = ai_handler
        self._bot = None
        self._loop = None
        self._thread: threading.Thread | None = None
        self._running = False

    def connect(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, name="Discord-Worker", daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        self._running = False
        if self._loop and self._bot:
            try:
                asyncio.run_coroutine_threadsafe(self._bot.close(), self._loop).result(timeout=5)
            except Exception:
                pass
        self._bot = None
        self._loop = None

    # ── internal ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            import discord
        except ImportError:
            self.log("[Discord] discord.py not installed — run: pip install discord.py")
            self._running = False
            return

        cfg = self.get_config()
        token = cfg.get("discord_token", "").strip()
        if not token:
            self.log("[Discord] No bot token configured — enter one in Connection Settings.")
            self._running = False
            return

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        intents = discord.Intents.default()
        intents.message_content = True
        self._bot = discord.Client(intents=intents)

        bot = self._bot
        loop = self._loop
        ai = self._ai
        get_config = self.get_config
        log = self.log

        @bot.event
        async def on_ready():
            log(f"[Discord] Connected as {bot.user} (ID: {bot.user.id})")

        @bot.event
        async def on_message(message):
            if message.author == bot.user:
                return

            cfg = get_config()
            try:
                channel_id = int(cfg.get("discord_channel_id", 0) or 0)
            except ValueError:
                return

            if message.channel.id != channel_id:
                return

            trigger = cfg.get("discord_trigger", "All messages")
            if not DiscordClient._is_triggered(trigger, bot.user, message):
                return

            discord_prompt = cfg.get("discord_prompt", "").strip() or None
            log(f"[Discord] {message.author.name}: {message.content}")

            channel = message.channel

            def reply_cb(text: str) -> None:
                asyncio.run_coroutine_threadsafe(channel.send(text), loop)

            ai.handle(message.author.name, message.content,
                      reply_cb=reply_cb, prompt_override=discord_prompt)

        try:
            self._loop.run_until_complete(bot.start(token))
        except Exception as exc:
            if self._running:
                self.log(f"[Discord] Error: {exc}")
        finally:
            self._running = False
            try:
                self._loop.close()
            except Exception:
                pass

    @staticmethod
    def _is_triggered(trigger: str, bot_user, message) -> bool:
        """Return True if the message meets the trigger-mode condition."""
        if trigger in ("All messages", "All messages + mentions + replies"):
            return True
        if trigger == "@mention only":
            return bot_user in message.mentions
        if trigger == "@mention + replies":
            is_mention = bot_user in message.mentions
            is_reply = (
                message.reference is not None
                and getattr(getattr(message.reference, "resolved", None), "author", None) == bot_user
            )
            return is_mention or is_reply
        return False
```

- [ ] **Step 2: Add asyncio import at the top of the file**

Find the existing imports block (around line 28–42). Add `import asyncio` after `from __future__ import annotations`:

The existing line is:
```python
from __future__ import annotations

import json
```

Change to:
```python
from __future__ import annotations

import asyncio
import json
```

- [ ] **Step 3: Run trigger-mode tests — all should pass now**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && .venv/bin/python -m pytest tests/test_discord_integration.py -v
```

Expected: all 10 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add twitch_bot.py tests/test_discord_integration.py
git commit -m "feat: add DiscordClient class with asyncio event loop and trigger-mode filtering"
```

---

## Task 5: Add Discord UI to Connection Settings and header status label

**Files:**
- Modify: `twitch_bot.py:668-815` (`_build_connection`)
- Modify: `twitch_bot.py:1021-1058` (`_build_header`)
- Modify: `twitch_bot.py:573` (`__init__` — new instance vars)

- [ ] **Step 1: Add Discord prompt instance vars to __init__**

In `TwitchBotApp.__init__`, find the block where prompt-related vars are declared (around line 604):
```python
        # Thread-safe system-prompt cache: written by GUI thread, read by AI worker
        self._prompt_lock:  threading.Lock = threading.Lock()
        self._prompt_cache: str = ""
```

Add immediately after it:
```python
        # Discord-specific prompt cache (same thread-safety pattern as _prompt_cache)
        self._discord_prompt_lock:  threading.Lock = threading.Lock()
        self._discord_prompt_cache: str = ""
```

- [ ] **Step 2: Add _discord service handle to __init__**

In `__init__`, find:
```python
        # Service handles
        self._irc: TwitchIRCClient | None = None
        self._tts: TTSEngine | None = None
        self._ai:  AIResponseHandler | None = None
```

Add `self._discord` after `self._ai`:
```python
        # Service handles
        self._irc:     TwitchIRCClient | None = None
        self._tts:     TTSEngine | None = None
        self._ai:      AIResponseHandler | None = None
        self._discord: DiscordClient | None = None
```

- [ ] **Step 3: Add Discord status label to _build_header()**

Find `_build_header` (around line 1021). The current last label is `_lbl_conn_status` at column 4. Add a Discord status label at column 5, inserting after the `_lbl_conn_status` block:

Find:
```python
        self._lbl_conn_status = ctk.CTkLabel(
            hdr, text="● Disconnected", text_color=OFF_FG,
            font=ctk.CTkFont(size=12),
        )
        self._lbl_conn_status.grid(row=0, column=4, padx=(0, 14), pady=10)
```

Replace with:
```python
        self._lbl_conn_status = ctk.CTkLabel(
            hdr, text="Twitch: ● Off", text_color=OFF_FG,
            font=ctk.CTkFont(size=12),
        )
        self._lbl_conn_status.grid(row=0, column=4, padx=(0, 8), pady=10)

        self._lbl_discord_status = ctk.CTkLabel(
            hdr, text="Discord: ● Off", text_color=OFF_FG,
            font=ctk.CTkFont(size=12),
        )
        self._lbl_discord_status.grid(row=0, column=5, padx=(0, 14), pady=10)
```

Also update the two places that set `_lbl_conn_status` text to use the new "Twitch: ●" prefix. Find in `_connect()` (around line 1197):
```python
        self._lbl_conn_status.configure(text="● Connecting…", text_color="#f39c12")
```
Replace with:
```python
        self._lbl_conn_status.configure(text="Twitch: ● Connecting…", text_color="#f39c12")
```

Find in `_disconnect()` (around line 1205):
```python
        self._lbl_conn_status.configure(text="● Disconnected", text_color=OFF_FG)
```
Replace with:
```python
        self._lbl_conn_status.configure(text="Twitch: ● Off", text_color=OFF_FG)
```

Find in `_dispatch()` (around line 1231):
```python
        self.after(0, lambda: self._lbl_conn_status.configure(
            text="● Connected", text_color=ON_FG))
```
Replace with:
```python
        self.after(0, lambda: self._lbl_conn_status.configure(
            text="Twitch: ● On", text_color=ON_FG))
```

- [ ] **Step 4: Add Discord section to _build_connection()**

In `_build_connection()`, find the very end of the method — the last line before the method ends (after the Piper TTS section, around line 815):
```python
        # Connect / Disconnect live in the main window header bar
```

Insert the entire Discord section after that comment:

```python
        # ── Discord Bot ───────────────────────────────────────────────────────
        section("Discord Bot")

        field("Bot Token",  "e_discord_token",
              default=e.get("DISCORD_TOKEN", ""),
              placeholder="Bot xxxxxxxxxxxxxxxxxxxx…", secret=True)
        field("Channel ID", "e_discord_channel_id",
              default=e.get("DISCORD_CHANNEL_ID", ""),
              placeholder="123456789012345678")

        # Trigger mode dropdown
        ctk.CTkLabel(tab, text="Trigger Mode", anchor="e").grid(
            row=r, column=0, sticky="e", padx=(14, 8), pady=5)
        _saved_trigger = e.get("DISCORD_TRIGGER", DiscordClient.TRIGGER_MODES[0])
        if _saved_trigger not in DiscordClient.TRIGGER_MODES:
            _saved_trigger = DiscordClient.TRIGGER_MODES[0]
        self._discord_trigger_combo = ctk.CTkComboBox(
            tab, values=DiscordClient.TRIGGER_MODES, width=280,
        )
        self._discord_trigger_combo.set(_saved_trigger)
        self._discord_trigger_combo.grid(row=r, column=1, sticky="w", padx=(0, 14), pady=5)
        r += 1

        # Shared prompt toggle
        ctk.CTkLabel(tab, text="System Prompt", anchor="e").grid(
            row=r, column=0, sticky="ne", padx=(14, 8), pady=(8, 2))
        _saved_shared = e.get("DISCORD_USE_SHARED_PROMPT", "true").lower() != "false"
        self._var_discord_shared_prompt = ctk.BooleanVar(value=_saved_shared)
        _prompt_frame = ctk.CTkFrame(tab, fg_color="transparent")
        _prompt_frame.grid(row=r, column=1, sticky="ew", padx=(0, 14), pady=5)
        _prompt_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkCheckBox(
            _prompt_frame, text="Use shared Twitch prompt",
            variable=self._var_discord_shared_prompt,
            command=self._toggle_discord_prompt_box,
        ).grid(row=0, column=0, sticky="w")
        r += 1

        # Discord-specific prompt textbox (hidden when shared prompt is active)
        self._discord_prompt_box = ctk.CTkTextbox(tab, height=80)
        _saved_dprompt = e.get("DISCORD_PROMPT", "")
        if _saved_dprompt:
            self._discord_prompt_box.insert("1.0", _saved_dprompt)
        self._discord_prompt_box.grid(
            row=r, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 4))
        r += 1

        # Hide prompt box if using shared prompt
        if _saved_shared:
            self._discord_prompt_box.grid_remove()

        # Discord Connect / Disconnect buttons
        _dc_btn_frame = ctk.CTkFrame(tab, fg_color="transparent")
        _dc_btn_frame.grid(row=r, column=1, sticky="w", padx=(0, 14), pady=(6, 10))
        self._btn_discord_connect = ctk.CTkButton(
            _dc_btn_frame, text="Connect Discord", width=140,
            fg_color=_GREEN[0], hover_color=_GREEN[1],
            command=self._discord_connect,
        )
        self._btn_discord_connect.pack(side="left", padx=(0, 8))
        self._btn_discord_disconnect = ctk.CTkButton(
            _dc_btn_frame, text="Disconnect", width=110,
            fg_color=_RED[0], hover_color=_RED[1], state="disabled",
            command=self._discord_disconnect,
        )
        self._btn_discord_disconnect.pack(side="left")
        r += 1
```

- [ ] **Step 5: Add _toggle_discord_prompt_box helper** (called when the shared-prompt checkbox is toggled)

Find `_build_connection` method's end or add near other `_build_*` helpers. Insert after `_build_connection` ends:

```python
    def _toggle_discord_prompt_box(self) -> None:
        if self._var_discord_shared_prompt.get():
            self._discord_prompt_box.grid_remove()
        else:
            self._discord_prompt_box.grid()
```

- [ ] **Step 6: Verify the app launches without errors**

```bash
DISPLAY=:0 .venv/bin/python twitch_bot.py &
sleep 3 && kill %1
```

Expected: no Python exceptions printed to terminal.

- [ ] **Step 7: Commit**

```bash
git add twitch_bot.py
git commit -m "feat: add Discord UI section to Connection Settings and status label to header"
```

---

## Task 6: Add Discord service lifecycle to TwitchBotApp

**Files:**
- Modify: `twitch_bot.py` — add `_get_discord_cfg`, `_discord_connect`, `_discord_disconnect`, update `_sync_prompt_cache`, `_stop_services`

- [ ] **Step 1: Add _get_discord_cfg() method**

Insert after `_get_irc_creds()` (around line 1181):

```python
    def _get_discord_cfg(self) -> dict:
        use_shared = self._var_discord_shared_prompt.get()
        if use_shared:
            discord_prompt = ""  # empty = use shared prompt (resolved in DiscordClient)
        else:
            with self._discord_prompt_lock:
                discord_prompt = self._discord_prompt_cache
        return {
            "discord_token":      self.e_discord_token.get().strip(),
            "discord_channel_id": self.e_discord_channel_id.get().strip(),
            "discord_trigger":    self._discord_trigger_combo.get(),
            "discord_prompt":     discord_prompt,
        }
```

- [ ] **Step 2: Add _discord_connect() and _discord_disconnect() methods**

Insert after `_disconnect()` (around line 1207):

```python
    def _discord_connect(self) -> None:
        if self._discord:
            self._discord.disconnect()
        self._save_env(log=False)
        ai = self._ai
        if not ai:
            self._log("[Discord] Start the AI handler first (app must be running).")
            return
        self._discord = DiscordClient(
            get_config=self._get_discord_cfg,
            log=self._log,
            ai_handler=ai,
        )
        self._discord.connect()
        self._btn_discord_connect.configure(state="disabled")
        self._btn_discord_disconnect.configure(state="normal")
        self._lbl_discord_status.configure(text="Discord: ● Connecting…", text_color="#f39c12")

    def _discord_disconnect(self) -> None:
        if self._discord:
            self._discord.disconnect()
            self._discord = None
        self._btn_discord_connect.configure(state="normal")
        self._btn_discord_disconnect.configure(state="disabled")
        self._lbl_discord_status.configure(text="Discord: ● Off", text_color=OFF_FG)
        self._log("[Discord] Disconnected.")
```

- [ ] **Step 3: Update _stop_services() to also stop Discord**

Find `_stop_services()` (around line 1139):
```python
    def _stop_services(self) -> None:
        if self._tts:
            self._tts.stop()
            self._tts = None
        if self._ai:
            self._ai.stop()
            self._ai = None
```

Replace with:
```python
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

- [ ] **Step 4: Update _sync_prompt_cache() to also sync Discord prompt**

Find `_sync_prompt_cache()` (around line 1170):
```python
    def _sync_prompt_cache(self) -> None:
        """Snapshot the system-prompt textbox on the GUI thread for safe worker access."""
        text = self._system_prompt.get("1.0", "end-1c")
        with self._prompt_lock:
            self._prompt_cache = text
```

Replace with:
```python
    def _sync_prompt_cache(self) -> None:
        """Snapshot both prompt textboxes on the GUI thread for safe worker access."""
        text = self._system_prompt.get("1.0", "end-1c")
        with self._prompt_lock:
            self._prompt_cache = text
        discord_text = self._discord_prompt_box.get("1.0", "end-1c")
        with self._discord_prompt_lock:
            self._discord_prompt_cache = discord_text
```

- [ ] **Step 5: Update Discord status label when bot confirms ready**

The `on_ready` event logs the connection but doesn't update the GUI status label. Add a queue-safe status update. In `DiscordClient._run()`, find the `on_ready` handler:

```python
        @bot.event
        async def on_ready():
            log(f"[Discord] Connected as {bot.user} (ID: {bot.user.id})")
```

This is already queue-safe (log() puts into `_log_queue`). To update the status label, pass an `on_ready_cb` callback. The cleanest way is to add an `on_status_change` callable to `DiscordClient.__init__`. Update `DiscordClient.__init__`:

```python
    def __init__(self, get_config, log, ai_handler: "AIResponseHandler", on_ready_cb=None) -> None:
        self.get_config = get_config
        self.log = log
        self._ai = ai_handler
        self._on_ready_cb = on_ready_cb  # callable() — called on GUI thread when bot connects
        self._bot = None
        self._loop = None
        self._thread: threading.Thread | None = None
        self._running = False
```

In `DiscordClient._run()`, update `on_ready` to call the callback via the GUI `after()`:

Since `DiscordClient` doesn't have access to the tkinter `after()`, the cleanest approach is to have `on_ready_cb` put a message into `_log_queue` which `_poll_logs` handles. But that only works for log messages.

Instead, use the `log` callable and have `TwitchBotApp._poll_logs` detect a special sentinel. **Simpler approach:** Just use `self.after(0, cb)` from within `_discord_connect()`. Pass a no-arg callable to `DiscordClient` that updates the label:

Update `_discord_connect()` so the callback is wired:

```python
    def _discord_connect(self) -> None:
        if self._discord:
            self._discord.disconnect()
        self._save_env(log=False)
        ai = self._ai
        if not ai:
            self._log("[Discord] Start the AI handler first (app must be running).")
            return

        def on_ready_cb():
            self.after(0, lambda: self._lbl_discord_status.configure(
                text="Discord: ● On", text_color=ON_FG))

        self._discord = DiscordClient(
            get_config=self._get_discord_cfg,
            log=self._log,
            ai_handler=ai,
            on_ready_cb=on_ready_cb,
        )
        self._discord.connect()
        self._btn_discord_connect.configure(state="disabled")
        self._btn_discord_disconnect.configure(state="normal")
        self._lbl_discord_status.configure(text="Discord: ● Connecting…", text_color="#f39c12")
```

And in `DiscordClient._run()`, update `on_ready`:
```python
        on_ready_cb = self._on_ready_cb

        @bot.event
        async def on_ready():
            log(f"[Discord] Connected as {bot.user} (ID: {bot.user.id})")
            if on_ready_cb:
                on_ready_cb()
```

- [ ] **Step 6: Verify app still launches cleanly**

```bash
DISPLAY=:0 .venv/bin/python twitch_bot.py &
sleep 3 && kill %1
```

Expected: no Python exceptions.

- [ ] **Step 7: Commit**

```bash
git add twitch_bot.py
git commit -m "feat: add Discord service lifecycle, config getter, and prompt cache sync"
```

---

## Task 7: Update _save_env() / _load_env() and auto-connect on startup

**Files:**
- Modify: `twitch_bot.py:1487-1504` (`_save_env`)
- Modify: `twitch_bot.py:620-626` (`__init__` auto-connect block)

- [ ] **Step 1: Add Discord fields to _save_env()**

Find `_save_env()` (around line 1487). The current `lines` list ends with:
```python
            f"PIPER_CONFIG={self.e_piper_cfg.get().strip()}",
```

Add the Discord fields after it, before the closing `]`:
```python
            f"DISCORD_TOKEN={self.e_discord_token.get().strip()}",
            f"DISCORD_CHANNEL_ID={self.e_discord_channel_id.get().strip()}",
            f"DISCORD_TRIGGER={self._discord_trigger_combo.get()}",
            f"DISCORD_USE_SHARED_PROMPT={'true' if self._var_discord_shared_prompt.get() else 'false'}",
            f"DISCORD_PROMPT={self._discord_prompt_box.get('1.0', 'end-1c')}",
```

- [ ] **Step 2: Add Discord auto-connect on startup**

Find the auto-connect block in `__init__` (around line 624):
```python
        # Auto-connect if all credentials are already saved
        if all(self._env.get(k) for k in ("TWITCH_CHANNEL", "TWITCH_USERNAME", "TWITCH_TOKEN")):
            self.after(800, self._connect)
```

Add Discord auto-connect after it:
```python
        # Auto-connect if all credentials are already saved
        if all(self._env.get(k) for k in ("TWITCH_CHANNEL", "TWITCH_USERNAME", "TWITCH_TOKEN")):
            self.after(800, self._connect)

        if self._env.get("DISCORD_TOKEN") and self._env.get("DISCORD_CHANNEL_ID"):
            self.after(1200, self._discord_connect)
```

- [ ] **Step 3: Run all tests**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot" && .venv/bin/python -m pytest tests/test_discord_integration.py -v
```

Expected: all 10 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add twitch_bot.py
git commit -m "feat: persist Discord settings to .env and auto-connect on startup"
```

---

## Task 8: Manual smoke test

- [ ] **Step 1: Launch the app**

```bash
DISPLAY=:0 .venv/bin/python twitch_bot.py
```

- [ ] **Step 2: Open Connection Settings (⚙ gear button) and scroll to Discord Bot section**

Verify: Bot Token field (masked), Channel ID field, Trigger Mode dropdown, "Use shared Twitch prompt" checkbox, Discord-specific prompt textbox (visible when checkbox is unchecked), Connect Discord / Disconnect buttons.

- [ ] **Step 3: Enter a Discord bot token and channel ID, click Connect Discord**

Verify: Console shows `[Discord] Connecting…` then `[Discord] Connected as <botname>`, header status label changes to `Discord: ● On`.

- [ ] **Step 4: Send a message in the configured Discord channel**

Verify: Console shows `[Discord] <username>: <message>` then `[Discord AI] → <reply>`, and the bot posts the reply in Discord.

- [ ] **Step 5: Test trigger modes**

Switch Trigger Mode to `@mention only`. Send a plain message — confirm no AI response. Send a message @mentioning the bot — confirm AI responds.

- [ ] **Step 6: Test Discord-specific prompt**

Uncheck "Use shared Twitch prompt", enter a custom Discord prompt, click Connect Discord. Send a message and verify the AI uses the Discord-specific persona (check console reply).

- [ ] **Step 7: Test Twitch + Discord running simultaneously**

Connect Twitch and Discord at the same time. Send messages to both — confirm both respond independently.

- [ ] **Step 8: Test .env persistence**

Close and relaunch the app. Verify Discord token and channel ID fields are pre-filled. If auto-connect fires, Discord status should go green without manual interaction.

- [ ] **Step 9: Commit any final fixes, then push**

```bash
git add -p  # stage only intentional changes
git commit -m "fix: <describe any fixes found during smoke test>"
git push
```
