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
from tkinter import filedialog

import customtkinter as ctk
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

# ── Global theme ─────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

TWITCH_HOST     = "irc.chat.twitch.tv"
TWITCH_PORT     = 6667
RECONNECT_DELAY = 5      # seconds between auto-reconnect attempts

# Colour constants (CTkButton accepts (normal, hover) tuples as fg_color)
_GREEN = ("#1a7f37", "#15662d")
_RED   = ("#c0392b", "#922b21")
ON_FG  = "#2ecc71"
OFF_FG = "#e74c3c"

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

    def __init__(self, enabled_var: ctk.BooleanVar) -> None:
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

    handle(username, message) enqueues work; a single daemon thread makes the
    blocking HTTP call and pipes the reply text to TTSEngine.speak().
    """

    def __init__(self, get_config, log, tts: TTSEngine) -> None:
        self.get_config = get_config   # callable → dict(endpoint, model, system_prompt, tts_ai)
        self.log = log
        self.tts = tts
        self._q: queue.Queue[tuple | None] = queue.Queue()
        threading.Thread(target=self._worker, name="AI-Worker", daemon=True).start()

    def handle(self, username: str, message: str, reply_cb=None, prompt_override: str | None = None) -> None:
        self._q.put((username, message, reply_cb, prompt_override))

    def stop(self) -> None:
        self._q.put(None)

    # ── worker ───────────────────────────────────────────────────────────────

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            username, message, reply_cb, prompt_override = item
            self._query(username, message, reply_cb=reply_cb, prompt_override=prompt_override)

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
# TwitchBotApp  (main window)
# ══════════════════════════════════════════════════════════════════════════════
class TwitchBotApp(ctk.CTk):
    """
    Main application window.  All GUI construction, event routing, and
    service lifecycle management lives here.
    """

    def __init__(self) -> None:
        super().__init__()
        self.title("Twitch Interactive Bot")
        self.geometry("1140x780")
        self.minsize(960, 640)

        # Paths next to this script
        _here = os.path.dirname(os.path.abspath(__file__))
        self._prompts_dir   = os.path.join(_here, "prompts")
        self._env_path      = os.path.join(_here, ".env")
        self._settings_path = os.path.join(_here, "settings.json")
        os.makedirs(self._prompts_dir, exist_ok=True)
        self._presets_dir = os.path.join(_here, "plays_presets")
        os.makedirs(self._presets_dir, exist_ok=True)

        # Load persisted connection settings (populated before _build_ui so
        # _build_connection can use them as field defaults)
        self._env = self._load_env()

        # Runtime toggle state
        self.game_input_enabled = ctk.BooleanVar(value=False)
        self.ai_enabled         = ctk.BooleanVar(value=False)

        # Command map: {"!cmd": {"key": str, "duration": float}}
        self.command_map: dict[str, dict] = {}

        # AI message counter (how many messages since last AI response)
        self._ai_counter = 0

        # Thread-safe log queue — only the GUI thread reads from it
        self._log_queue: queue.Queue[str] = queue.Queue()

        # Thread-safe system-prompt cache: written by GUI thread, read by AI worker
        self._prompt_lock:  threading.Lock = threading.Lock()
        self._prompt_cache: str = ""

        # Service handles
        self._irc: TwitchIRCClient | None = None
        self._tts: TTSEngine | None = None
        self._ai:  AIResponseHandler | None = None

        self._build_ui()
        self._apply_settings(self._load_settings())
        self._start_services()
        self._poll_logs()

        self._log("[System] Ready.")
        self._log_platform_info()

        # Auto-save settings every 10 seconds so state persists even if
        # the process is killed rather than closed gracefully
        self._autosave()

        # Auto-connect if all credentials are already saved
        if all(self._env.get(k) for k in ("TWITCH_CHANNEL", "TWITCH_USERNAME", "TWITCH_TOKEN")):
            self.after(800, self._connect)

    # ── Platform diagnostics ─────────────────────────────────────────────────

    def _log_platform_info(self) -> None:
        libs = []
        libs.append("pygame ✓" if HAS_PYGAME else "pygame ✗ (no audio)")
        libs.append("pydirectinput ✓" if HAS_PYDIRECTINPUT else "pydirectinput ✗")
        libs.append("pynput ✓" if HAS_PYNPUT else "pynput ✗")
        self._log(f"[System] Libraries: {', '.join(libs)}")
        if not HAS_PYDIRECTINPUT and not HAS_PYNPUT:
            self._log("[System] WARNING: No input library found — Twitch Plays disabled.")

    # ══════════════════════════════════════════════════════════════════════════
    # UI Construction
    # ══════════════════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._create_settings_window()

        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 4))
        main.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(0, weight=1)

        plays_frame = ctk.CTkFrame(main, corner_radius=8)
        plays_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self._build_plays(plays_frame)

        ai_frame = ctk.CTkFrame(main, corner_radius=8)
        ai_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self._build_ai(ai_frame)

        self._build_console_section()

    # ── Connection tab ────────────────────────────────────────────────────────

    def _build_connection(self, tab: ctk.CTkFrame) -> None:
        tab.grid_columnconfigure(1, weight=1)
        r = 0

        def section(title: str) -> None:
            nonlocal r
            ctk.CTkLabel(
                tab, text=title,
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color="#6fa3d0",
            ).grid(row=r, column=0, columnspan=3, sticky="w", padx=14, pady=(18, 4))
            r += 1

        def field(label: str, attr: str, default: str = "", placeholder: str = "",
                  secret: bool = False) -> None:
            nonlocal r
            ctk.CTkLabel(tab, text=label, anchor="e").grid(
                row=r, column=0, sticky="e", padx=(14, 8), pady=5)
            e = ctk.CTkEntry(
                tab,
                placeholder_text=placeholder,
                width=450,
                show="●" if secret else "",
            )
            if default:
                e.insert(0, default)
            e.grid(row=r, column=1, sticky="ew", padx=(0, 14), pady=5)
            setattr(self, attr, e)
            r += 1

        def file_field(label: str, attr: str, placeholder: str = "", default: str = "") -> None:
            nonlocal r
            ctk.CTkLabel(tab, text=label, anchor="e").grid(
                row=r, column=0, sticky="e", padx=(14, 8), pady=5)
            fr = ctk.CTkFrame(tab, fg_color="transparent")
            fr.grid(row=r, column=1, sticky="ew", padx=(0, 14), pady=5)
            fr.grid_columnconfigure(0, weight=1)
            e = ctk.CTkEntry(fr, placeholder_text=placeholder)
            if default:
                e.insert(0, default)
            e.grid(row=0, column=0, sticky="ew")
            ctk.CTkButton(
                fr, text="Browse", width=74,
                command=lambda _e=e: self._browse(_e),
            ).grid(row=0, column=1, padx=(6, 0))
            setattr(self, attr, e)
            r += 1

        e = self._env  # shorthand

        # ── Twitch IRC ────────────────────────────────────────────────────────
        section("Twitch IRC")
        field("Channel",      "e_channel",
              default=e.get("TWITCH_CHANNEL", ""),  placeholder="channelname")
        field("Bot Username", "e_username",
              default=e.get("TWITCH_USERNAME", ""), placeholder="mybotname")

        # Client ID row — entry + "Get OAuth Token" button on the same line
        ctk.CTkLabel(tab, text="Client ID", anchor="e").grid(
            row=r, column=0, sticky="e", padx=(14, 8), pady=5)
        cid_frame = ctk.CTkFrame(tab, fg_color="transparent")
        cid_frame.grid(row=r, column=1, sticky="ew", padx=(0, 14), pady=5)
        cid_frame.grid_columnconfigure(0, weight=1)
        self.e_client_id = ctk.CTkEntry(
            cid_frame, placeholder_text="your Twitch app client ID")
        if e.get("TWITCH_CLIENT_ID"):
            self.e_client_id.insert(0, e["TWITCH_CLIENT_ID"])
        self.e_client_id.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            cid_frame, text="Get OAuth Token ↗", width=160,
            command=self._get_oauth_token,
        ).grid(row=0, column=1, padx=(8, 0))
        r += 1

        field("OAuth Token",  "e_token",
              default=e.get("TWITCH_TOKEN", ""),
              placeholder="oauth:xxxxxxxxxxxxxxxx", secret=True)

        # ── AI Provider ───────────────────────────────────────────────────────
        section("AI Provider")

        # Provider selector
        _saved_provider = e.get("LLM_PROVIDER", "Ollama")
        if _saved_provider not in _PROVIDERS:
            _saved_provider = "Ollama"
        ctk.CTkLabel(tab, text="Provider", anchor="e").grid(
            row=r, column=0, sticky="e", padx=(14, 8), pady=5)
        self._provider_combo = ctk.CTkComboBox(
            tab, values=list(_PROVIDERS.keys()), width=200,
            command=self._on_provider_change,
        )
        self._provider_combo.set(_saved_provider)
        self._provider_combo.grid(row=r, column=1, sticky="w", padx=(0, 14), pady=5)
        r += 1

        # API Key (hidden for local providers)
        self._api_key_lbl = ctk.CTkLabel(tab, text="API Key", anchor="e")
        self._api_key_lbl.grid(row=r, column=0, sticky="e", padx=(14, 8), pady=5)
        self.e_api_key = ctk.CTkEntry(tab, width=450, show="●",
                                      placeholder_text="sk-... / AIza... / xai-...")
        if e.get("LLM_API_KEY"):
            self.e_api_key.insert(0, e["LLM_API_KEY"])
        self.e_api_key.grid(row=r, column=1, sticky="ew", padx=(0, 14), pady=5)
        self._api_key_row = r
        r += 1

        # Endpoint
        field(
            "API Endpoint", "e_endpoint",
            default=e.get("LLM_ENDPOINT", "http://localhost:11434/v1/chat/completions"),
            placeholder="http://localhost:11434/v1/chat/completions",
        )

        # Model — combo populated by _fetch_models()
        ctk.CTkLabel(tab, text="Model Name", anchor="e").grid(
            row=r, column=0, sticky="e", padx=(14, 8), pady=5)
        _mf = ctk.CTkFrame(tab, fg_color="transparent")
        _mf.grid(row=r, column=1, sticky="ew", padx=(0, 14), pady=5)
        _mf.grid_columnconfigure(0, weight=1)
        _saved_model = e.get("LLM_MODEL", "llama3") or "llama3"
        self.e_model = ctk.CTkComboBox(_mf, values=[_saved_model], width=300)
        self.e_model.set(_saved_model)
        self.e_model.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            _mf, text="Refresh", width=74,
            command=self._fetch_models,
        ).grid(row=0, column=1, padx=(6, 0))
        r += 1

        # Apply initial show/hide for API key based on saved provider
        self._set_api_key_visibility(_PROVIDERS[_saved_provider]["needs_key"])

        # ── Piper TTS ─────────────────────────────────────────────────────────
        # Auto-detect the bundled piper binary when no .env entry exists yet
        _local_piper = os.path.join(os.path.dirname(self._env_path), "piper", "piper")
        _default_piper = _local_piper if os.path.exists(_local_piper) else ""

        section("Piper TTS")
        file_field("Piper Executable",     "e_piper_exe",
                   placeholder="piper  or  /path/to/piper",
                   default=e.get("PIPER_EXE") or _default_piper)
        file_field("Voice Model  (.onnx)", "e_piper_model",
                   placeholder="/path/to/voice.onnx",
                   default=e.get("PIPER_MODEL", ""))
        file_field("Model Config (.json)", "e_piper_cfg",
                   placeholder="/path/to/voice.onnx.json  (optional)",
                   default=e.get("PIPER_CONFIG", ""))

        # Connect / Disconnect live in the main window header bar

    # ── Twitch Plays tab ──────────────────────────────────────────────────────

    def _build_plays(self, tab: ctk.CTkFrame) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        # Master toggle
        hdr = ctk.CTkFrame(tab, corner_radius=8)
        hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        ctk.CTkLabel(
            hdr, text="Game Inputs Active",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left", padx=14, pady=10)
        ctk.CTkSwitch(
            hdr, text="",
            variable=self.game_input_enabled,
            onvalue=True, offvalue=False,
        ).pack(side="left")
        self._lbl_plays = ctk.CTkLabel(
            hdr, text="OFF", text_color=OFF_FG,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self._lbl_plays.pack(side="left", padx=10)
        self.game_input_enabled.trace_add(
            "write",
            lambda *_: self._lbl_plays.configure(
                text="ON"  if self.game_input_enabled.get() else "OFF",
                text_color=ON_FG if self.game_input_enabled.get() else OFF_FG,
            ),
        )

        # Add-mapping controls
        add = ctk.CTkFrame(tab, corner_radius=8)
        add.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))

        ctk.CTkLabel(add, text="Command:").pack(side="left", padx=(14, 4), pady=10)
        self._e_new_cmd = ctk.CTkEntry(add, placeholder_text="!jump", width=110)
        self._e_new_cmd.pack(side="left", padx=4)

        ctk.CTkLabel(add, text="Key:").pack(side="left", padx=(10, 4))
        self._e_new_key = ctk.CTkEntry(add, placeholder_text="space", width=90)
        self._e_new_key.pack(side="left", padx=4)

        ctk.CTkLabel(add, text="Hold (s):").pack(side="left", padx=(10, 4))
        self._e_new_dur = ctk.CTkEntry(add, placeholder_text="0.5", width=65)
        self._e_new_dur.pack(side="left", padx=4)

        ctk.CTkButton(
            add, text="+ Add Mapping", width=130,
            fg_color=_GREEN[0], hover_color=_GREEN[1],
            command=self._add_mapping,
        ).pack(side="left", padx=14)

        # Scrollable command list
        self._plays_scroll = ctk.CTkScrollableFrame(
            tab, label_text="Active Command Mappings",
        )
        self._plays_scroll.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self._plays_scroll.grid_columnconfigure(0, weight=1)
        self._refresh_plays()

    # ── AI Interaction tab ────────────────────────────────────────────────────

    def _build_ai(self, tab: ctk.CTkFrame) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(3, weight=1)

        # Master toggle
        hdr = ctk.CTkFrame(tab, corner_radius=8)
        hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        ctk.CTkLabel(
            hdr, text="AI Chat Reading Active",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left", padx=14, pady=10)
        ctk.CTkSwitch(
            hdr, text="",
            variable=self.ai_enabled,
            onvalue=True, offvalue=False,
        ).pack(side="left")
        self._lbl_ai = ctk.CTkLabel(
            hdr, text="OFF", text_color=OFF_FG,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self._lbl_ai.pack(side="left", padx=10)
        self.ai_enabled.trace_add(
            "write",
            lambda *_: self._lbl_ai.configure(
                text="ON"  if self.ai_enabled.get() else "OFF",
                text_color=ON_FG if self.ai_enabled.get() else OFF_FG,
            ),
        )

        # Trigger Conditions
        opts = ctk.CTkFrame(tab, corner_radius=8)
        opts.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))
        opts.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            opts, text="Trigger Conditions",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#6fa3d0",
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 6))

        # Every N messages
        row_n = ctk.CTkFrame(opts, fg_color="transparent")
        row_n.grid(row=1, column=0, sticky="w", padx=14, pady=3)
        self._var_every_n_enabled = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(row_n, text="Every", variable=self._var_every_n_enabled,
                        width=78).pack(side="left")
        self._e_every_n = ctk.CTkEntry(row_n, width=55)
        self._e_every_n.insert(0, "5")
        self._e_every_n.pack(side="left", padx=6)
        ctk.CTkLabel(row_n, text="messages").pack(side="left")

        # @bot mentions
        row_m = ctk.CTkFrame(opts, fg_color="transparent")
        row_m.grid(row=2, column=0, sticky="w", padx=14, pady=3)
        self._var_mentions = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row_m, text="@bot mentions", variable=self._var_mentions).pack(side="left")

        # Bits cheer
        row_b = ctk.CTkFrame(opts, fg_color="transparent")
        row_b.grid(row=3, column=0, sticky="w", padx=14, pady=3)
        self._var_trigger_bits = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row_b, text="Bits cheer  ≥",
                        variable=self._var_trigger_bits, width=138).pack(side="left")
        self._e_min_bits = ctk.CTkEntry(row_b, width=65, placeholder_text="100")
        self._e_min_bits.pack(side="left", padx=6)
        ctk.CTkLabel(row_b, text="bits").pack(side="left")

        # Channel Point redeem
        row_p = ctk.CTkFrame(opts, fg_color="transparent")
        row_p.grid(row=4, column=0, sticky="w", padx=14, pady=(6, 2))
        self._var_trigger_points = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row_p, text="Channel Point redeem",
                        variable=self._var_trigger_points).pack(side="left")

        row_pid = ctk.CTkFrame(opts, fg_color="transparent")
        row_pid.grid(row=5, column=0, sticky="ew", padx=(42, 14), pady=(2, 0))
        row_pid.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(row_pid, text="Reward ID:", anchor="e").grid(
            row=0, column=0, sticky="e", padx=(0, 8))
        self._e_reward_id = ctk.CTkEntry(
            row_pid, placeholder_text="leave blank for any redemption")
        self._e_reward_id.grid(row=0, column=1, sticky="ew")

        ctk.CTkLabel(
            opts,
            text="Tip: reward IDs are logged to the Console tab when a redemption arrives.",
            text_color="gray", font=ctk.CTkFont(size=11),
        ).grid(row=6, column=0, sticky="w", padx=(42, 14), pady=(2, 8))

        # Divider
        ctk.CTkFrame(opts, height=1, fg_color="#333").grid(
            row=7, column=0, sticky="ew", padx=14, pady=4)

        # TTS option
        row_tts = ctk.CTkFrame(opts, fg_color="transparent")
        row_tts.grid(row=8, column=0, sticky="w", padx=14, pady=(4, 10))
        self._var_tts_ai = ctk.BooleanVar(value=True)
        ctk.CTkLabel(row_tts, text="Speak AI replies via TTS:").pack(side="left", padx=(0, 8))
        ctk.CTkCheckBox(row_tts, text="", variable=self._var_tts_ai).pack(side="left")

        # System prompt header with dropdown + save
        prompt_hdr = ctk.CTkFrame(tab, fg_color="transparent")
        prompt_hdr.grid(row=2, column=0, sticky="ew", padx=10, pady=(8, 2))
        prompt_hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            prompt_hdr, text="System Prompt",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=4)

        ctrl_bar = ctk.CTkFrame(prompt_hdr, fg_color="transparent")
        ctrl_bar.grid(row=0, column=1, sticky="e")

        self._prompt_combo = ctk.CTkComboBox(
            ctrl_bar, width=200,
            values=self._prompt_values(),
            command=self._on_prompt_selected,
        )
        self._prompt_combo.set("")
        self._prompt_combo.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            ctrl_bar, text="Save", width=80,
            command=self._save_prompt,
        ).pack(side="left")

        self._system_prompt = ctk.CTkTextbox(
            tab, font=ctk.CTkFont(family="Courier", size=12),
        )
        self._system_prompt.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self._system_prompt.insert(
            "1.0",
            "You are an energetic Twitch chat bot named ChatBot. "
            "You read chat messages and respond in 1-2 short, lively sentences. "
            "Be hype, funny, and keep it PG-13. "
            "Never start a reply with 'Sure', 'Of course', or 'Certainly'.",
        )

    # ── Header bar ────────────────────────────────────────────────────────────

    def _build_header(self) -> None:
        hdr = ctk.CTkFrame(self, corner_radius=0, height=48)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)
        hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            hdr, text="Twitch Interactive Bot",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=16, pady=10)

        self._lbl_conn_status = ctk.CTkLabel(
            hdr, text="● Disconnected", text_color=OFF_FG,
            font=ctk.CTkFont(size=12),
        )
        self._lbl_conn_status.grid(row=0, column=4, padx=(0, 14), pady=10)

        self._btn_disconnect = ctk.CTkButton(
            hdr, text="Disconnect", width=120, state="disabled",
            fg_color=_RED[0], hover_color=_RED[1],
            command=self._disconnect,
        )
        self._btn_disconnect.grid(row=0, column=3, padx=(0, 8), pady=10)

        self._btn_connect = ctk.CTkButton(
            hdr, text="Connect", width=110,
            fg_color=_GREEN[0], hover_color=_GREEN[1],
            command=self._connect,
        )
        self._btn_connect.grid(row=0, column=2, padx=(0, 6), pady=10)

        ctk.CTkButton(
            hdr, text="⏹ Panic", width=90,
            fg_color="#8b0000", hover_color="#b22222",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._panic_tts,
        ).grid(row=0, column=1, padx=(0, 10), pady=10)

    # ── Settings window (hidden until cog is clicked) ─────────────────────────

    def _create_settings_window(self) -> None:
        win = ctk.CTkToplevel(self)
        win.title("Connection Settings")
        win.geometry("640x700")
        win.resizable(False, True)
        win.transient(self)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        self._settings_win = win

        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(0, weight=1)
        inner = ctk.CTkFrame(win, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="nsew")
        inner.grid_columnconfigure(1, weight=1)
        self._build_connection(inner)

        win.withdraw()

    def _open_settings(self) -> None:
        self._settings_win.deiconify()
        self._settings_win.lift()
        self._settings_win.focus()

    # ── Console section (pinned to bottom of main window) ─────────────────────

    def _build_console_section(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 0))
        bar.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            bar, text="Console",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            bar, text="Clear Console", width=120,
            command=self._clear_console,
        ).grid(row=0, column=1)

        self._console = ctk.CTkTextbox(
            self, height=190,
            state="disabled",
            font=ctk.CTkFont(family="Courier", size=12),
            text_color="#c8c8c8",
            wrap="word",
        )
        self._console.grid(row=3, column=0, sticky="ew", padx=10, pady=(2, 0))

        self._build_manual_prompt()

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=5, column=0, sticky="ew", padx=10, pady=(6, 8))

        ctk.CTkButton(
            footer, text="⚙", width=38, height=32,
            font=ctk.CTkFont(size=15),
            fg_color="#2b2d42",
            hover_color="#3d3f5c",
            command=self._open_settings,
        ).pack(side="left")
        ctk.CTkLabel(
            footer, text="Connection Settings",
            text_color="gray", font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=8)

    # ══════════════════════════════════════════════════════════════════════════
    # Service lifecycle
    # ══════════════════════════════════════════════════════════════════════════

    def _start_services(self) -> None:
        """Create TTS + AI handler workers using current GUI config."""
        self._tts = TTSEngine(get_config=self._get_tts_cfg, log=self._log)
        self._ai  = AIResponseHandler(
            get_config=self._get_ai_cfg,
            log=self._log,
            tts=self._tts,
        )

    def _stop_services(self) -> None:
        if self._tts:
            self._tts.stop()
            self._tts = None
        if self._ai:
            self._ai.stop()
            self._ai = None

    # ── Config getters (called on worker threads)
    #    CTkEntry/CTkComboBox/BooleanVar.get() are GIL-safe scalar reads.
    #    CTkTextbox.get(index, index) is a Tcl round-trip — use _prompt_cache instead. ────

    def _get_tts_cfg(self) -> dict:
        return {
            "piper_exe":   self.e_piper_exe.get().strip()   or "piper",
            "model_path":  self.e_piper_model.get().strip(),
            "config_path": self.e_piper_cfg.get().strip(),
        }

    def _get_ai_cfg(self) -> dict:
        with self._prompt_lock:
            prompt = self._prompt_cache
        return {
            "provider":      self._provider_combo.get(),
            "endpoint":      self.e_endpoint.get().strip(),
            "model":         self.e_model.get().strip(),
            "api_key":       self.e_api_key.get().strip(),
            "system_prompt": prompt,
            "tts_ai":        self._var_tts_ai.get(),
        }

    def _sync_prompt_cache(self) -> None:
        """Snapshot the system-prompt textbox on the GUI thread for safe worker access."""
        text = self._system_prompt.get("1.0", "end-1c")
        with self._prompt_lock:
            self._prompt_cache = text

    def _get_irc_creds(self) -> dict:
        return {
            "channel":  self.e_channel.get().strip(),
            "username": self.e_username.get().strip(),
            "token":    self.e_token.get().strip(),
        }

    # ══════════════════════════════════════════════════════════════════════════
    # IRC connect / disconnect
    # ══════════════════════════════════════════════════════════════════════════

    def _connect(self) -> None:
        self._save_env(log=True)
        self._irc = TwitchIRCClient(
            get_creds=self._get_irc_creds,
            log=self._log,
            on_message=self._dispatch,
        )
        self._irc.connect()
        self._btn_connect.configure(state="disabled")
        self._btn_disconnect.configure(state="normal")
        self._lbl_conn_status.configure(text="● Connecting…", text_color="#f39c12")

    def _disconnect(self) -> None:
        if self._irc:
            self._irc.disconnect()
            self._irc = None
        self._btn_connect.configure(state="normal")
        self._btn_disconnect.configure(state="disabled")
        self._lbl_conn_status.configure(text="● Disconnected", text_color=OFF_FG)
        self._log("[System] Disconnected.")

    def _panic_tts(self) -> None:
        if self._tts:
            self._tts.panic()
        self._log("[TTS] Panic — audio stopped and queue cleared.")

    # ══════════════════════════════════════════════════════════════════════════
    # Message dispatch  (called from IRC thread)
    # ══════════════════════════════════════════════════════════════════════════

    def _dispatch(self, username: str, message: str, bits: int = 0, reward_id: str = "") -> None:
        """
        Central dispatcher — routes each PRIVMSG to the Plays system and/or
        the AI system.  Runs on the IRC background thread; must never touch
        Tkinter widgets directly.  Use _log() which is queue-safe.
        """
        tag = f"  [{bits} bits]" if bits else (f"  [channel points]" if reward_id else "")
        self._log(f"[Chat] {username}{tag}: {message}")
        if reward_id:
            self._log(f"[Chat] Reward ID: {reward_id}")
        self._route_plays(username, message)
        self._route_ai(username, message, bits, reward_id)

        # Update connection status indicator the first time a message arrives
        self.after(0, lambda: self._lbl_conn_status.configure(
            text="● Connected", text_color=ON_FG))

    def _route_plays(self, username: str, message: str) -> None:
        word = message.strip().split()[0].lower() if message.strip() else ""
        if word not in self.command_map or not self.game_input_enabled.get():
            return
        entry = self.command_map[word]
        self._log(
            f"[Plays] {username} → {word}  (key '{entry['key']}' × {entry['duration']}s)"
        )
        GameInputController(self.game_input_enabled).execute(
            entry["key"], entry["duration"]
        )

    def _route_ai(self, username: str, message: str, bits: int = 0, reward_id: str = "") -> None:
        if not self.ai_enabled.get():
            return

        triggered = False

        if self._var_mentions.get():
            bot = self.e_username.get().lower()
            if bot and bot in message.lower():
                triggered = True

        if self._var_trigger_bits.get() and bits > 0:
            try:
                min_bits = max(1, int(self._e_min_bits.get().strip() or "1"))
            except ValueError:
                min_bits = 1
            if bits >= min_bits:
                triggered = True
                self._log(f"[AI] Bits trigger: {username} cheered {bits} bits")

        if self._var_trigger_points.get() and reward_id:
            required = self._e_reward_id.get().strip()
            if not required or reward_id.lower() == required.lower():
                triggered = True
                self._log(f"[AI] Points trigger: {username} redeemed (ID: {reward_id})")
            else:
                self._log(f"[AI] Unmatched redemption — reward ID: {reward_id}")

        if self._var_every_n_enabled.get():
            try:
                every_n = max(1, int(self._e_every_n.get()))
            except ValueError:
                every_n = 5
            self._ai_counter += 1
            if self._ai_counter >= every_n:
                self._ai_counter = 0
                triggered = True

        ai = self._ai
        if triggered and ai:
            ai.handle(username, message)

    # ══════════════════════════════════════════════════════════════════════════
    # Command-mapping UI
    # ══════════════════════════════════════════════════════════════════════════

    def _add_mapping(self) -> None:
        cmd = self._e_new_cmd.get().strip().lower()
        key = self._e_new_key.get().strip().lower()
        try:
            dur = float(self._e_new_dur.get().strip() or "0.3")
        except ValueError:
            dur = 0.3

        if not cmd or not key:
            return
        if not cmd.startswith("!"):
            cmd = f"!{cmd}"

        self.command_map[cmd] = {"key": key, "duration": round(dur, 3)}
        for e in (self._e_new_cmd, self._e_new_key, self._e_new_dur):
            e.delete(0, "end")
        self._refresh_plays()
        self._log(f"[Plays] Added:  {cmd}  →  '{key}'  for {dur}s")

    def _remove_mapping(self, cmd: str) -> None:
        self.command_map.pop(cmd, None)
        self._refresh_plays()
        self._log(f"[Plays] Removed: {cmd}")

    def _refresh_plays(self) -> None:
        for w in self._plays_scroll.winfo_children():
            w.destroy()

        if not self.command_map:
            ctk.CTkLabel(
                self._plays_scroll,
                text="No mappings yet.  Add one using the fields above.",
                text_color="gray",
            ).grid(row=0, column=0, pady=24)
            return

        for i, (cmd, info) in enumerate(sorted(self.command_map.items())):
            row = ctk.CTkFrame(
                self._plays_scroll,
                fg_color=("#2b2b2b" if i % 2 == 0 else "#1f1f1f"),
                corner_radius=6,
            )
            row.grid(row=i, column=0, sticky="ew", pady=2, padx=4)
            row.grid_columnconfigure(1, weight=1)

            ctk.CTkLabel(
                row, text=cmd,
                text_color="#3daee9",
                font=ctk.CTkFont(family="Courier", weight="bold"),
                width=120, anchor="w",
            ).grid(row=0, column=0, padx=14, pady=8)

            ctk.CTkLabel(
                row,
                text=f"→   press  '{info['key']}'   for  {info['duration']}s",
                anchor="w",
            ).grid(row=0, column=1, padx=6, pady=8, sticky="w")

            ctk.CTkButton(
                row, text="Remove", width=88,
                fg_color=_RED[0], hover_color=_RED[1],
                command=lambda c=cmd: self._remove_mapping(c),
            ).grid(row=0, column=2, padx=12, pady=8)

    def _list_presets(self) -> list[str]:
        if not os.path.isdir(self._presets_dir):
            return []
        return sorted(f[:-5] for f in os.listdir(self._presets_dir) if f.endswith(".json"))

    def _preset_values(self) -> list[str]:
        return ["+ New Preset"] + self._list_presets()

    def _on_preset_selected(self, name: str) -> None:
        if name == "+ New Preset":
            self._new_preset()
            return
        path = os.path.join(self._presets_dir, f"{name}.json")
        if not os.path.exists(path):
            self._log(f"[Presets] File not found: {name}.json")
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self._log(f"[Presets] Failed to load '{name}': {e}")
            return
        self.command_map = {k: v for k, v in data.items()}
        self._refresh_plays()
        self._log(f"[Presets] Loaded: {name}")

    def _save_preset(self) -> None:
        name = self._preset_combo.get().strip()
        if not name or name == "+ New Preset":
            self._log("[Presets] Select or type a preset name first.")
            return
        safe = re.sub(r'[^\w\s\-]', '', name).strip()
        if not safe:
            self._log("[Presets] Invalid name — use letters, numbers, spaces, or dashes.")
            return
        path = os.path.join(self._presets_dir, f"{safe}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.command_map, f, indent=2)
        self._preset_combo.configure(values=self._preset_values())
        self._preset_combo.set(safe)
        self._log(f"[Presets] Saved → {safe}")

    def _new_preset(self) -> None:
        dialog = ctk.CTkInputDialog(text="Name for new preset:", title="New Preset")
        name = dialog.get_input()
        if not name:
            self._preset_combo.configure(values=self._preset_values())
            self._preset_combo.set("")
            return
        safe = re.sub(r'[^\w\s\-]', '', name.strip()).strip()
        if not safe:
            self._log("[Presets] Invalid name — use letters, numbers, spaces, or dashes.")
            self._preset_combo.configure(values=self._preset_values())
            self._preset_combo.set("")
            return
        path = os.path.join(self._presets_dir, f"{safe}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.command_map, f, indent=2)
        self._preset_combo.configure(values=self._preset_values())
        self._preset_combo.set(safe)
        self._log(f"[Presets] Created preset '{safe}' with current mappings.")

    # ══════════════════════════════════════════════════════════════════════════
    # Settings persistence (settings.json)
    # ══════════════════════════════════════════════════════════════════════════

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

    def _load_settings(self) -> dict:
        s = dict(self._SETTINGS_DEFAULTS)
        if os.path.exists(self._settings_path):
            try:
                with open(self._settings_path, encoding="utf-8") as f:
                    s.update(json.load(f))
            except Exception:
                pass
        return s

    def _autosave(self) -> None:
        self._save_settings()
        self._save_env()
        self.after(10_000, self._autosave)

    def _save_settings(self) -> None:
        try:
            every_n  = int(self._e_every_n.get().strip()   or "5")
        except ValueError:
            every_n  = 5
        try:
            min_bits = int(self._e_min_bits.get().strip()  or "100")
        except ValueError:
            min_bits = 100

        data = {
            "ai_enabled":       self.ai_enabled.get(),
            "trigger_every_n":  self._var_every_n_enabled.get(),
            "every_n":          every_n,
            "trigger_mentions": self._var_mentions.get(),
            "trigger_bits":     self._var_trigger_bits.get(),
            "min_bits":         min_bits,
            "trigger_points":   self._var_trigger_points.get(),
            "reward_id":        self._e_reward_id.get().strip(),
            "tts_ai":           self._var_tts_ai.get(),
            "plays_enabled":    self.game_input_enabled.get(),
            "command_map":      self.command_map,
            "last_prompt":      self._prompt_combo.get().strip(),
        }
        with open(self._settings_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _apply_settings(self, s: dict) -> None:
        # AI Interaction
        self.ai_enabled.set(s["ai_enabled"])
        self._var_every_n_enabled.set(s["trigger_every_n"])
        self._e_every_n.delete(0, "end")
        self._e_every_n.insert(0, str(s["every_n"]))
        self._var_mentions.set(s["trigger_mentions"])
        self._var_trigger_bits.set(s["trigger_bits"])
        self._e_min_bits.delete(0, "end")
        self._e_min_bits.insert(0, str(s["min_bits"]))
        self._var_trigger_points.set(s["trigger_points"])
        self._e_reward_id.delete(0, "end")
        self._e_reward_id.insert(0, s["reward_id"])
        self._var_tts_ai.set(s["tts_ai"])
        # Twitch Plays
        self.game_input_enabled.set(s["plays_enabled"])
        self.command_map = {k: v for k, v in s.get("command_map", {}).items()}
        self._refresh_plays()
        # Restore last loaded prompt
        self._prompt_combo.configure(values=self._prompt_values())
        last = s.get("last_prompt", "")
        if last:
            self._prompt_combo.set(last)
            self._on_prompt_selected(last)

    # ══════════════════════════════════════════════════════════════════════════
    # OAuth helper
    # ══════════════════════════════════════════════════════════════════════════

    def _get_oauth_token(self) -> None:
        client_id = self.e_client_id.get().strip()
        if not client_id:
            self._log("[Auth] Enter your Client ID before requesting a token.")
            return

        scope = "chat:read+chat:edit"
        url = (
            "https://id.twitch.tv/oauth2/authorize"
            f"?client_id={client_id}"
            "&redirect_uri=http://localhost"
            "&response_type=token"
            f"&scope={scope}"
        )

        self._log("[Auth] ── OAuth Authorization URL (copy and open in your browser) ──")
        self._log(url)
        self._log("[Auth] Steps after authorizing:")
        self._log("[Auth]   1. Click Authorize on the Twitch page.")
        self._log("[Auth]   2. Browser redirects to localhost (error page is fine).")
        self._log("[Auth]   3. Copy the value between 'access_token=' and '&scope' in the address bar.")
        self._log("[Auth]   4. Go to the Connection tab, paste it into the OAuth Token field.")
        self._log("[Auth]   5. Click Connect.")

    # ══════════════════════════════════════════════════════════════════════════
    # .env persistence
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

    def _save_env(self, *, log: bool = False) -> None:
        lines = [
            f"TWITCH_CHANNEL={self.e_channel.get().strip()}",
            f"TWITCH_USERNAME={self.e_username.get().strip()}",
            f"TWITCH_CLIENT_ID={self.e_client_id.get().strip()}",
            f"TWITCH_TOKEN={self.e_token.get().strip()}",
            f"LLM_PROVIDER={self._provider_combo.get()}",
            f"LLM_ENDPOINT={self.e_endpoint.get().strip()}",
            f"LLM_MODEL={self.e_model.get().strip()}",
            f"LLM_API_KEY={self.e_api_key.get().strip()}",
            f"PIPER_EXE={self.e_piper_exe.get().strip()}",
            f"PIPER_MODEL={self.e_piper_model.get().strip()}",
            f"PIPER_CONFIG={self.e_piper_cfg.get().strip()}",
        ]
        with open(self._env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        if log:
            self._log("[System] Connection settings saved to .env")

    # ══════════════════════════════════════════════════════════════════════════
    # Prompt save / load
    # ══════════════════════════════════════════════════════════════════════════

    def _list_prompts(self) -> list[str]:
        """Return sorted prompt names (filenames without .txt) from the prompts dir."""
        if not os.path.isdir(self._prompts_dir):
            return []
        return sorted(f[:-4] for f in os.listdir(self._prompts_dir) if f.endswith(".txt"))

    def _prompt_values(self) -> list[str]:
        """Dropdown values: special New entry first, then existing prompts."""
        return ["+ New Prompt"] + self._list_prompts()

    def _on_prompt_selected(self, name: str) -> None:
        if name == "+ New Prompt":
            self._new_prompt()
            return
        if not name:
            return
        path = os.path.join(self._prompts_dir, f"{name}.txt")
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self._system_prompt.delete("1.0", "end")
        self._system_prompt.insert("1.0", content)
        self._sync_prompt_cache()
        self._log(f"[Prompts] Loaded ← {name}")

    def _save_prompt(self) -> None:
        name = self._prompt_combo.get().strip()
        if not name or name == "+ New Prompt":
            self._log("[Prompts] Select or type a prompt name first.")
            return
        safe = re.sub(r'[^\w\s\-]', '', name).strip()
        if not safe:
            self._log("[Prompts] Invalid name — use letters, numbers, spaces, or dashes.")
            return
        path = os.path.join(self._prompts_dir, f"{safe}.txt")
        content = self._system_prompt.get("1.0", "end-1c")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        self._prompt_combo.configure(values=self._prompt_values())
        self._prompt_combo.set(safe)
        self._log(f"[Prompts] Saved → {safe}")

    def _new_prompt(self) -> None:
        dialog = ctk.CTkInputDialog(text="Name for new prompt:", title="New Prompt")
        name = dialog.get_input()
        if not name:
            self._prompt_combo.configure(values=self._prompt_values())
            self._prompt_combo.set("")
            return
        safe = re.sub(r'[^\w\s\-]', '', name.strip()).strip()
        if not safe:
            self._log("[Prompts] Invalid name — use letters, numbers, spaces, or dashes.")
            self._prompt_combo.configure(values=self._prompt_values())
            self._prompt_combo.set("")
            return
        self._system_prompt.delete("1.0", "end")
        self._prompt_combo.configure(values=self._prompt_values())
        self._prompt_combo.set(safe)
        self._sync_prompt_cache()
        self._log(f"[Prompts] New prompt '{safe}' — edit and click Save.")

    # ══════════════════════════════════════════════════════════════════════════
    # Thread-safe logging
    # ══════════════════════════════════════════════════════════════════════════

    def _log(self, msg: str) -> None:
        """Queue a log line from any thread."""
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_queue.put(f"[{ts}]  {msg}")

    def _poll_logs(self) -> None:
        """
        Drain the log queue on the GUI thread every 80 ms.
        Batches all pending messages in one pass to minimise widget updates.
        """
        lines: list[str] = []
        try:
            while True:
                lines.append(self._log_queue.get_nowait())
        except queue.Empty:
            pass

        if lines:
            self._console.configure(state="normal")
            self._console.insert("end", "\n".join(lines) + "\n")
            self._console.see("end")
            self._console.configure(state="disabled")

        self._sync_prompt_cache()
        self.after(80, self._poll_logs)

    def _clear_console(self) -> None:
        self._console.configure(state="normal")
        self._console.delete("1.0", "end")
        self._console.configure(state="disabled")

    def _send_manual_prompt(self) -> None:
        msg = self._e_manual_prompt.get().strip()
        if not msg:
            return
        self._e_manual_prompt.delete(0, "end")
        self._log(f"[Host]: {msg}")
        ai = self._ai
        if not ai:
            self._log("[Host] AI not initialised — restart the app.")
            return
        ai.handle("Host", msg)

    def _build_manual_prompt(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=4, column=0, sticky="ew", padx=10, pady=(2, 0))
        bar.grid_columnconfigure(0, weight=1)

        self._e_manual_prompt = ctk.CTkEntry(
            bar, placeholder_text="Message the AI...",
        )
        self._e_manual_prompt.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._e_manual_prompt.bind("<Return>", lambda _: self._send_manual_prompt())

        ctk.CTkButton(
            bar, text="Send", width=80,
            command=self._send_manual_prompt,
        ).grid(row=0, column=1)

    # ══════════════════════════════════════════════════════════════════════════
    # Utilities
    # ══════════════════════════════════════════════════════════════════════════

    def _on_provider_change(self, provider: str) -> None:
        info = _PROVIDERS.get(provider, _PROVIDERS["Ollama"])
        self._set_api_key_visibility(info["needs_key"])
        self.e_endpoint.delete(0, "end")
        self.e_endpoint.insert(0, info["endpoint"])
        self._fetch_models()

    def _set_api_key_visibility(self, visible: bool) -> None:
        if visible:
            self._api_key_lbl.grid()
            self.e_api_key.grid()
        else:
            self._api_key_lbl.grid_remove()
            self.e_api_key.grid_remove()

    def _fetch_models(self) -> None:
        import urllib.parse
        provider = self._provider_combo.get()
        api_key  = self.e_api_key.get().strip()

        # Claude: hardcoded list, no network call needed
        if provider == "Claude":
            self.after(0, lambda: self._apply_model_list(_CLAUDE_MODELS))
            return

        endpoint = self.e_endpoint.get().strip()
        if not endpoint:
            return
        parsed = urllib.parse.urlparse(endpoint)
        base   = f"{parsed.scheme}://{parsed.netloc}"

        def _worker() -> None:
            models: list[str] = []

            if provider == "Gemini":
                try:
                    url  = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
                    resp = requests.get(url, timeout=10)
                    resp.raise_for_status()
                    raw = resp.json().get("models", [])
                    models = [
                        m["name"].removeprefix("models/")
                        for m in raw
                        if "generateContent" in m.get("supportedGenerationMethods", [])
                    ]
                except Exception:
                    pass
            elif provider in ("OpenAI", "Grok"):
                try:
                    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                    resp    = requests.get(f"{base}/v1/models", headers=headers, timeout=10)
                    resp.raise_for_status()
                    models  = [item["id"] for item in resp.json().get("data", [])]
                    models.sort()
                except Exception:
                    pass
            else:
                # Ollama / LM Studio — try OpenAI list then Ollama native
                for url, key, sub in [
                    (f"{base}/v1/models", "data",   "id"),
                    (f"{base}/api/tags",  "models", "name"),
                ]:
                    try:
                        resp = requests.get(url, timeout=5)
                        resp.raise_for_status()
                        models = [item[sub] for item in resp.json().get(key, []) if sub in item]
                        if models:
                            break
                    except Exception:
                        continue

            if models:
                self.after(0, lambda m=models: self._apply_model_list(m))
            else:
                self._log("[AI] Could not fetch model list — check endpoint and API key.")

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_model_list(self, models: list[str]) -> None:
        current = self.e_model.get()
        self.e_model.configure(values=models)
        self.e_model.set(current if current in models else models[0])
        self._log(f"[AI] Loaded {len(models)} model(s).")

    @staticmethod
    def _browse(entry: ctk.CTkEntry) -> None:
        path = filedialog.askopenfilename()
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)

    # ══════════════════════════════════════════════════════════════════════════
    # Clean shutdown
    # ══════════════════════════════════════════════════════════════════════════

    def on_closing(self) -> None:
        self._save_settings()
        self._save_env()
        self._disconnect()
        self._stop_services()
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = TwitchBotApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
