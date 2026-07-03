#!/usr/bin/env python3
"""
twitch_bot.py — Twitch Interactive Bot
Customizable stream interaction tool with Twitch Plays, AI chat responses,
and local Piper TTS. Inspired by DougDoug's stream setups.

═══════════════════════════════════════════════════════════
THREADING MODEL
═══════════════════════════════════════════════════════════
  Main thread   │ GUI (CustomTkinter mainloop + after() callbacks)
  IRC thread    │ TwitchIRCClient._run() — raw TCP socket reader
                │   auto-reconnects on failure; parses PRIVMSG → _dispatch()
  AI thread     │ AIResponseHandler._worker() — HTTP to local LLM
                │   dequeues (username, message) pairs, POSTs, enqueues TTS
  TTS thread    │ TTSEngine._worker() — Piper subprocess + pygame playback
                │   dequeues text, runs piper binary, plays .wav
  Input threads │ Short-lived daemon threads per key press (GameInputController)

Cross-thread communication:
  • _log_queue (Queue[str])  bridges all background threads to GUI console
    via self.after(80, _poll_logs) on the main thread — zero GUI calls
    from worker threads.
  • AI and TTS each have their own Queue for work items.
  • game_input_enabled / ai_enabled are BooleanVar; .get() is GIL-safe for reads.
═══════════════════════════════════════════════════════════
"""

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

TWITCH_HOST     = "irc.chat.twitch.tv"
TWITCH_PORT     = 6667
RECONNECT_DELAY = 5      # seconds between auto-reconnect attempts

# ── AI provider definitions ───────────────────────────────────────────────────
_PROVIDERS: dict[str, dict] = {
    "Ollama":    {"endpoint": "http://localhost:11434/v1/chat/completions", "needs_key": False, "fmt": "openai"},
    "LM Studio": {"endpoint": "http://localhost:1234/v1/chat/completions",  "needs_key": False, "fmt": "openai"},
    "OpenAI":    {"endpoint": "https://api.openai.com/v1/chat/completions", "needs_key": True,  "fmt": "openai"},
    "Grok":      {"endpoint": "https://api.x.ai/v1/chat/completions",       "needs_key": True,  "fmt": "openai"},
    "Gemini":    {"endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                  "needs_key": True, "fmt": "openai"},
    "Claude":    {"endpoint": "https://api.anthropic.com/v1/messages",      "needs_key": True,  "fmt": "anthropic"},
}

_CLAUDE_MODELS = [
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
]


class _BoolGetter:
    """Minimal stand-in for ctk.BooleanVar used by GameInputController."""
    __slots__ = ("_v",)
    def __init__(self, v: bool) -> None: self._v = v
    def get(self) -> bool: return self._v


# ══════════════════════════════════════════════════════════════════════════════
# GameInputController
# ══════════════════════════════════════════════════════════════════════════════
class GameInputController:
    """
    Simulates keyboard input for game control.

    Uses pydirectinput (DirectX/Raw-input compatible) when available (Windows),
    otherwise falls back to pynput which works on Linux/macOS.

    Each key press is delegated to a short-lived daemon thread so the IRC
    reader is never blocked waiting for a hold-duration to elapse.
    """

    def __init__(self, enabled_var: _BoolGetter) -> None:
        self.enabled_var = enabled_var

    def execute(self, key: str, duration: float) -> None:
        """Fire-and-forget: hold `key` for `duration` seconds."""
        if not self.enabled_var.get():
            return
        t = threading.Thread(target=self._press, args=(key, duration), daemon=True)
        t.start()

    def _press(self, key: str, duration: float) -> None:
        duration = max(0.01, duration)

        if HAS_PYDIRECTINPUT:
            try:
                pydirectinput.keyDown(key)
                time.sleep(duration)
                pydirectinput.keyUp(key)
                return
            except Exception:
                pass  # fall through to pynput

        if HAS_PYNPUT:
            try:
                _pynput_kb.press(key)
                time.sleep(duration)
                _pynput_kb.release(key)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# TTSEngine
# ══════════════════════════════════════════════════════════════════════════════
class TTSEngine:
    """
    Text-to-Speech via Piper TTS (subprocess) + pygame playback.

    speak(text) enqueues work; a single daemon thread dequeues and processes
    serially so audio clips never overlap.  The subprocess writes a temp .wav
    which pygame loads as a Sound object; we block in the TTS thread (not the
    GUI thread) until playback finishes, then delete the temp file.
    """

    def __init__(self, get_config, log) -> None:
        self.get_config = get_config   # callable → dict(piper_exe, model_path, config_path)
        self.log = log
        self._q: queue.Queue[str | None] = queue.Queue()
        self._stop_event = threading.Event()
        self._current_proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        threading.Thread(target=self._worker, name="TTS-Worker", daemon=True).start()

    def speak(self, text: str) -> None:
        self._q.put(text)

    def stop(self) -> None:
        self._q.put(None)

    def panic(self) -> None:
        """Immediately silence all audio and drain the pending queue."""
        # Drain pending items; preserve any stop() sentinel so the worker can exit.
        saw_stop = False
        while True:
            try:
                item = self._q.get_nowait()
                if item is None:
                    saw_stop = True
            except queue.Empty:
                break
        if saw_stop:
            self._q.put(None)
        # Signal the play loop to abort
        self._stop_event.set()
        # Stop pygame playback
        if HAS_PYGAME:
            try:
                pygame.mixer.stop()
            except Exception:
                pass
        # Kill any running subprocess (Piper synthesis or system audio player)
        with self._proc_lock:
            if self._current_proc and self._current_proc.poll() is None:
                self._current_proc.kill()
                self._current_proc = None

    # ── worker ───────────────────────────────────────────────────────────────

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            if self._stop_event.is_set():
                self._stop_event.clear()  # consume panic — skip this item, resume on next
                continue
            self._synthesize(item)

    def _synthesize(self, text: str) -> None:
        cfg        = self.get_config()
        piper_exe  = cfg.get("piper_exe")   or "piper"
        model_path = cfg.get("model_path")  or ""
        cfg_path   = cfg.get("config_path") or ""

        if not model_path:
            self.log("[TTS] No voice model configured — skipping speech.")
            return

        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp_path = f.name

            if self._stop_event.is_set():
                return

            cmd = [piper_exe, "--model", model_path, "--output_file", tmp_path]
            if cfg_path:
                cmd += ["--config", cfg_path]

            with self._proc_lock:
                if self._stop_event.is_set():
                    return
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                self._current_proc = proc

            try:
                _, stderr_bytes = proc.communicate(input=text.encode("utf-8"), timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    threading.Thread(
                        target=proc.communicate, daemon=True,
                    ).start()
                self.log("[TTS] Piper synthesis timed out.")
                return
            finally:
                with self._proc_lock:
                    if self._current_proc is proc:
                        self._current_proc = None

            if self._stop_event.is_set():
                return

            if proc.returncode == -9:
                self.log("[TTS] Piper was killed unexpectedly (SIGKILL).")
                return

            if proc.returncode != 0:
                err = stderr_bytes.decode("utf-8", errors="replace").strip()
                self.log(f"[TTS] Piper error: {err}")
                return

            self._play(tmp_path)

        except FileNotFoundError:
            self.log(f"[TTS] Piper executable not found: '{piper_exe}'")
        except Exception as exc:
            self.log(f"[TTS] Unexpected error: {exc}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # System audio players tried in order when pygame.mixer is unavailable
    _SYSTEM_PLAYERS = ("pw-play", "paplay", "aplay", "ffplay", "mpv")

    def _play(self, wav_path: str) -> None:
        if self._stop_event.is_set():
            return

        if HAS_PYGAME:
            try:
                sound   = pygame.mixer.Sound(wav_path)
                channel = sound.play()
                while channel and channel.get_busy():
                    if self._stop_event.is_set():
                        channel.stop()
                        return
                    time.sleep(0.05)
                return
            except Exception as exc:
                self.log(f"[TTS] pygame error: {exc}")

        # pygame.mixer unavailable or failed — try system audio players
        for player in self._SYSTEM_PLAYERS:
            try:
                with self._proc_lock:
                    if self._stop_event.is_set():
                        return
                    proc = subprocess.Popen(
                        [player, wav_path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self._current_proc = proc
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    self.log(f"[TTS] {player} timed out.")
                finally:
                    with self._proc_lock:
                        if self._current_proc is proc:
                            self._current_proc = None
                if proc.returncode == 0 or self._stop_event.is_set():
                    return
            except FileNotFoundError:
                continue
            except Exception as exc:
                self.log(f"[TTS] {player} error: {exc}")

        self.log("[TTS] No working audio player found.")


# ══════════════════════════════════════════════════════════════════════════════
# AIResponseHandler
# ══════════════════════════════════════════════════════════════════════════════
class AIResponseHandler:
    """
    Sends chat messages to a local LLM via HTTP (OpenAI-compatible endpoint).
    Compatible with Ollama (:11434) and LM Studio (:1234) out of the box.

    handle(username, message, reply_cb, prompt_override) enqueues work; a single
    daemon thread makes the blocking HTTP call and pipes the reply text to
    TTSEngine.speak(). reply_cb, if provided, is called from the AI worker thread —
    it must not make GUI/CTk calls directly.
    """

    def __init__(self, get_config, log, tts: TTSEngine) -> None:
        self.get_config = get_config   # callable → dict(endpoint, model, system_prompt, tts_ai)
        self.log = log
        self.tts = tts
        self._q: queue.Queue[tuple | None] = queue.Queue()
        threading.Thread(target=self._worker, name="AI-Worker", daemon=True).start()

    def handle(self, username: str, message: str, reply_cb=None, prompt_override: str | None = None, use_tts: bool | None = None) -> None:
        self._q.put((username, message, reply_cb, prompt_override, use_tts))

    def stop(self) -> None:
        self._q.put(None)

    # ── worker ───────────────────────────────────────────────────────────────

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            username, message, reply_cb, prompt_override, use_tts = item
            self._query(username, message, reply_cb=reply_cb, prompt_override=prompt_override, use_tts=use_tts)

    def _query(self, username: str, message: str, reply_cb=None, prompt_override: str | None = None, use_tts: bool | None = None) -> None:
        cfg           = self.get_config()
        provider      = cfg.get("provider", "Ollama")
        endpoint      = cfg.get("endpoint")      or "http://localhost:11434/v1/chat/completions"
        model         = cfg.get("model")         or "llama3"
        api_key       = cfg.get("api_key",  "")
        system_prompt = (
            prompt_override if prompt_override is not None
            else cfg.get("system_prompt")
        ) or "You are a helpful Twitch chat bot."
        _use_tts      = use_tts if use_tts is not None else cfg.get("tts_ai", True)
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
            if _use_tts:
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

    def _query_openai(self, endpoint: str, model: str, api_key: str,
                      system_prompt: str, username: str, message: str) -> str:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": f"{username}: {message}"},
            ],
            "stream": False,
            "max_tokens": 1500,
        }
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=90)
        resp.raise_for_status()
        data   = resp.json()
        msg    = data["choices"][0]["message"]
        reply  = (msg.get("content") or "").strip()
        if not reply:
            finish = data["choices"][0].get("finish_reason", "")
            if finish == "length":
                self.log("[AI] Ran out of tokens mid-think — try a shorter system prompt or a non-reasoning model.")
        return reply

    def _query_anthropic(self, endpoint: str, model: str, api_key: str,
                         system_prompt: str, username: str, message: str) -> str:
        headers = {
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }
        payload = {
            "model":      model,
            "max_tokens": 1500,
            "system":     system_prompt,
            "messages":   [{"role": "user", "content": f"{username}: {message}"}],
        }
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=90)
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()


# ══════════════════════════════════════════════════════════════════════════════
# TwitchIRCClient
# ══════════════════════════════════════════════════════════════════════════════
class TwitchIRCClient:
    """
    Raw TCP connection to Twitch IRC.

    Runs entirely on a background daemon thread (_run → _session).
    Auto-reconnects indefinitely on any socket error until disconnect() is called.
    Handles PING/PONG keepalive so the 5-minute Twitch timeout is transparent.
    Parses PRIVMSG lines and calls on_message(username, text) for each chat message.
    """

    def __init__(self, get_creds, log, on_message) -> None:
        self.get_creds  = get_creds       # callable → dict(channel, username, token)
        self.log        = log
        self.on_message = on_message      # callback(username: str, message: str)
        self._sock: socket.socket | None = None
        self._running = False

    def connect(self) -> None:
        self._running = True
        threading.Thread(target=self._run, name="IRC-Reader", daemon=True).start()

    def disconnect(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def say(self, channel: str, text: str) -> None:
        """Send a chat message to the channel (called from any thread)."""
        sock = self._sock  # snapshot to avoid TOCTOU with disconnect()
        if sock:
            try:
                sock.sendall(f"PRIVMSG #{channel} :{text}\r\n".encode())
            except OSError as exc:
                self.log(f"[IRC] Send failed: {exc}")

    # ── reconnect loop ────────────────────────────────────────────────────────

    def _run(self) -> None:
        while self._running:
            try:
                self._session()
            except Exception as exc:
                if self._running:
                    self.log(f"[IRC] Disconnected ({exc}). Reconnecting in {RECONNECT_DELAY}s…")
                    time.sleep(RECONNECT_DELAY)

    def _session(self) -> None:
        creds   = self.get_creds()
        channel = creds.get("channel",  "").lower().strip()
        user    = creds.get("username", "").lower().strip()
        token   = creds.get("token",    "").strip()

        if not all([channel, user, token]):
            self.log("[IRC] Credentials incomplete — fill in all Connection fields and reconnect.")
            self._running = False
            return

        if not token.startswith("oauth:"):
            token = f"oauth:{token}"

        self._sock = socket.create_connection((TWITCH_HOST, TWITCH_PORT), timeout=15)
        self._sock.settimeout(300)  # 5-min idle timeout; we send PING to keep-alive

        for line in (
            f"PASS {token}",
            f"NICK {user}",
            "CAP REQ :twitch.tv/tags twitch.tv/commands",
            f"JOIN #{channel}",
        ):
            self._sock.sendall((line + "\r\n").encode())

        self.log(f"[IRC] Connected to #{channel} as {user}")

        buf = ""
        while self._running:
            try:
                chunk = self._sock.recv(4096).decode("utf-8", errors="replace")
            except socket.timeout:
                # Twitch requires a PING reply; we can also send our own PING
                self._sock.sendall(b"PING :tmi.twitch.tv\r\n")
                continue

            if not chunk:
                raise ConnectionResetError("Server closed the connection")

            buf += chunk
            # Split on \r\n; keep any trailing incomplete line in the buffer
            *complete, buf = buf.split("\r\n")
            for line in complete:
                self._handle(line.strip())

    def _handle(self, line: str) -> None:
        if not line:
            return
        if line.startswith("PING"):
            self._sock.sendall(b"PONG :tmi.twitch.tv\r\n")
            return

        # Parse IRCv3 tag block (@key=value;...) present on all Twitch messages
        tags: dict[str, str] = {}
        if line.startswith("@"):
            tag_str, _, line = line[1:].partition(" ")
            for pair in tag_str.split(";"):
                k, _, v = pair.partition("=")
                tags[k] = v

        m = re.search(r":(\w+)!\w+@\S+\.tmi\.twitch\.tv PRIVMSG #\S+ :(.+)", line)
        if m:
            bits      = int(tags.get("bits", 0) or 0)
            reward_id = tags.get("custom-reward-id", "")
            self.on_message(m.group(1), m.group(2).strip(), bits, reward_id)


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

    reply_cb is invoked from the AI worker thread — must not make GUI/CTk calls.
    """

    TRIGGER_MODES = [
        "All messages",
        "@mention only",
        "@mention + replies",
        "All messages + mentions + replies",
    ]

    def __init__(self, get_config, log, ai_handler: "AIResponseHandler", on_ready_cb=None, on_failure_cb=None) -> None:
        self.get_config = get_config  # callable → dict(discord_token, discord_channel_id, discord_trigger, discord_prompt)
        self.log = log
        self._ai = ai_handler
        self._on_ready_cb = on_ready_cb  # callable() — called when bot connects; safe to use self.after(0, ...) inside
        self._on_failure_cb = on_failure_cb  # callable() — called when connection fails
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
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=6)
        self._bot = None
        self._loop = None
        self._thread = None

    # ── internal ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        on_ready_cb = self._on_ready_cb
        on_failure_cb = self._on_failure_cb

        try:
            import discord
        except ImportError:
            self.log("[Discord] discord.py not installed — run: pip install discord.py")
            self._running = False
            if on_failure_cb:
                on_failure_cb()
            return

        cfg = self.get_config()
        token = cfg.get("discord_token", "").strip()
        if not token:
            self.log("[Discord] No bot token configured — enter one in Connection Settings.")
            self._running = False
            if on_failure_cb:
                on_failure_cb()
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
            cfg = get_config()
            try:
                channel_id = int(cfg.get("discord_channel_id", 0) or 0)
                if channel_id == 0:
                    log("[Discord] Warning: no channel ID configured — enter one in Connection Settings.")
            except ValueError:
                log("[Discord] Warning: invalid channel ID — enter a numeric channel ID in Connection Settings.")
            if on_ready_cb:
                on_ready_cb()

        @bot.event
        async def on_message(message):
            if message.author == bot.user:
                return

            cfg = get_config()
            try:
                channel_id = int(cfg.get("discord_channel_id", 0) or 0)
            except ValueError:
                return

            if channel_id == 0 or message.channel.id != channel_id:
                return

            trigger = cfg.get("discord_trigger", "All messages")
            if not DiscordClient._is_triggered(trigger, bot.user, message):
                return

            discord_prompt = cfg.get("discord_prompt", "").strip() or None
            log(f"[Discord] {message.author.name}: {message.content}")

            channel = message.channel

            def reply_cb(text: str) -> None:
                fut = asyncio.run_coroutine_threadsafe(channel.send(text), loop)
                fut.add_done_callback(
                    lambda f: log(f"[Discord] Send error: {f.exception()}")
                    if not f.cancelled() and f.exception() else None
                )

            ai.handle(message.author.name, message.content,
                      reply_cb=reply_cb, prompt_override=discord_prompt, use_tts=False)

        try:
            self._loop.run_until_complete(bot.start(token))
        except discord.LoginFailure:
            self.log("[Discord] Login failed — check your bot token.")
            if on_failure_cb:
                on_failure_cb()
        except Exception as exc:
            if self._running:
                self.log(f"[Discord] Error: {exc}")
            if on_failure_cb:
                on_failure_cb()
        finally:
            self._running = False
            _loop = self._loop
            self._loop = None
            if _loop is not None:
                try:
                    _loop.close()
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

    def _log_platform_info(self) -> None:
        libs = []
        libs.append("pygame ✓" if HAS_PYGAME else "pygame ✗ (no audio)")
        libs.append("pydirectinput ✓" if HAS_PYDIRECTINPUT else "pydirectinput ✗")
        libs.append("pynput ✓" if HAS_PYNPUT else "pynput ✗")
        self._log(f"[System] Libraries: {', '.join(libs)}")
        if not HAS_PYDIRECTINPUT and not HAS_PYNPUT:
            self._log("[System] WARNING: No input library found — Twitch Plays disabled.")

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
            use_shared     = self._config.get("discord_use_shared_prompt", True)
            discord_prompt = "" if use_shared else self._config.get("discord_prompt", "")
            return {
                "discord_token":      self._config.get("discord_token",      ""),
                "discord_channel_id": self._config.get("discord_channel_id", ""),
                "discord_trigger":    self._config.get("discord_trigger",    "All messages"),
                "discord_prompt":     discord_prompt,
            }

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
            c["command_map"] = dict(c["command_map"])
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
        try:
            self._save_settings()
            self._save_env()
        except Exception as exc:
            self._log(f"[System] Autosave failed: {exc}")
        t = threading.Timer(10, self._autosave)
        t.daemon = True
        t.start()

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
            cmds = data.get("commands")
            if not isinstance(cmds, dict):
                return _flask.jsonify({"error": "commands must be an object"}), 400
            commands = {
                str(k): v for k, v in cmds.items()
                if isinstance(v, dict) and isinstance(v.get("key"), str)
            }
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
            _INT_KEYS = {"every_n", "min_bits"}
            _BOOL_KEYS = {
                "trigger_every_n", "trigger_mentions", "trigger_bits",
                "trigger_points", "tts_ai", "discord_use_shared_prompt",
            }
            with self._config_lock:
                for k in _SETTINGS_KEYS:
                    if k in data:
                        if k in _INT_KEYS:
                            try:
                                self._config[k] = int(data[k])
                            except (TypeError, ValueError):
                                pass
                        elif k in _BOOL_KEYS:
                            self._config[k] = bool(data[k])
                        else:
                            self._config[k] = data[k]
                if "system_prompt" in data:
                    self._config["system_prompt"] = data["system_prompt"]
            self._save_env()
            self._log("[System] Settings saved.")
            return _flask.jsonify({"ok": True})

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
                            f"/v1beta/models?key={urllib.parse.quote(api_key, safe='')}")
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
        if self._ai:
            self._ai.stop()
            self._ai = None
        if self._tts:
            self._tts.stop()
            self._tts = None

    def _connect(self) -> None:
        irc = self._irc
        if irc:
            irc.disconnect()
            self._irc = None
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

    def _discord_connect(self) -> None:
        if self._discord:
            self._discord.disconnect()
        self._save_env()

        _inst: list = [None]

        def on_ready_cb() -> None:
            with self._config_lock:
                self._config["discord_status"] = "online"
            self._broadcast_status()

        def on_failure_cb() -> None:
            with self._config_lock:
                self._config["discord_status"] = "error"
                if self._discord is _inst[0]:
                    self._discord = None
            self._broadcast_status()

        with self._config_lock:
            self._config["discord_status"] = "connecting"
        self._broadcast_status()

        client = DiscordClient(
            get_config=self._get_discord_cfg,
            log=self._log,
            ai_handler=self._ai,
            on_ready_cb=on_ready_cb,
            on_failure_cb=on_failure_cb,
        )
        _inst[0] = client
        self._discord = client
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
        key = entry.get("key", "")
        duration = entry.get("duration", 0)
        if not key:
            self._log(f"[Plays] Bad entry for '{word}' — missing 'key'")
            return
        self._log(
            f"[Plays] {username} → {word}  "
            f"(key '{key}' × {duration}s)"
        )
        GameInputController(_BoolGetter(enabled)).execute(key, duration)

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
