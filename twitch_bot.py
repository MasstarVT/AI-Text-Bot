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
        threading.Thread(target=self._worker, name="TTS-Worker", daemon=True).start()

    def speak(self, text: str) -> None:
        self._q.put(text)

    def stop(self) -> None:
        self._q.put(None)

    # ── worker ───────────────────────────────────────────────────────────────

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
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

            cmd = [piper_exe, "--model", model_path, "--output_file", tmp_path]
            if cfg_path:
                cmd += ["--config", cfg_path]

            result = subprocess.run(
                cmd,
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0:
                err = result.stderr.decode("utf-8", errors="replace").strip()
                self.log(f"[TTS] Piper error: {err}")
                return

            self._play(tmp_path)

        except FileNotFoundError:
            self.log(f"[TTS] Piper executable not found: '{piper_exe}'")
        except subprocess.TimeoutExpired:
            self.log("[TTS] Piper synthesis timed out.")
        except Exception as exc:
            self.log(f"[TTS] Unexpected error: {exc}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _play(self, wav_path: str) -> None:
        if not HAS_PYGAME:
            self.log("[TTS] pygame unavailable — audio skipped (pip install pygame).")
            return
        try:
            sound   = pygame.mixer.Sound(wav_path)
            channel = sound.play()
            while channel and channel.get_busy():
                time.sleep(0.05)
        except Exception as exc:
            self.log(f"[TTS] Playback error: {exc}")


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
        self._q: queue.Queue[tuple[str, str] | None] = queue.Queue()
        threading.Thread(target=self._worker, name="AI-Worker", daemon=True).start()

    def handle(self, username: str, message: str) -> None:
        self._q.put((username, message))

    def stop(self) -> None:
        self._q.put(None)

    # ── worker ───────────────────────────────────────────────────────────────

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            self._query(*item)

    def _query(self, username: str, message: str) -> None:
        cfg           = self.get_config()
        endpoint      = cfg.get("endpoint")      or "http://localhost:11434/v1/chat/completions"
        model         = cfg.get("model")         or "llama3"
        system_prompt = cfg.get("system_prompt") or "You are a helpful Twitch chat bot."
        use_tts       = cfg.get("tts_ai", True)

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": f"{username}: {message}"},
            ],
            "stream": False,
            "max_tokens": 150,
        }

        try:
            resp = requests.post(endpoint, json=payload, timeout=30)
            resp.raise_for_status()
            data  = resp.json()
            reply = data["choices"][0]["message"]["content"].strip()
            self.log(f"[AI] → {reply}")
            if use_tts:
                self.tts.speak(reply)
        except requests.exceptions.ConnectionError:
            self.log("[AI] Cannot reach LLM server — is Ollama / LM Studio running?")
        except requests.exceptions.Timeout:
            self.log("[AI] LLM request timed out (>30 s).")
        except (KeyError, IndexError):
            self.log("[AI] Unexpected response format from LLM.")
        except Exception as exc:
            self.log(f"[AI] Error: {exc}")


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
        if self._sock:
            try:
                self._sock.sendall(f"PRIVMSG #{channel} :{text}\r\n".encode())
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

        # Standard PRIVMSG format (with or without leading @tags):
        # [@tags ]:user!user@user.tmi.twitch.tv PRIVMSG #channel :message
        m = re.search(r":(\w+)!\w+@\S+\.tmi\.twitch\.tv PRIVMSG #\S+ :(.+)", line)
        if m:
            self.on_message(m.group(1), m.group(2).strip())


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

        # Runtime toggle state
        self.game_input_enabled = ctk.BooleanVar(value=False)
        self.ai_enabled         = ctk.BooleanVar(value=False)

        # Command map: {"!cmd": {"key": str, "duration": float}}
        self.command_map: dict[str, dict] = {}

        # AI message counter (how many messages since last AI response)
        self._ai_counter = 0

        # Thread-safe log queue — only the GUI thread reads from it
        self._log_queue: queue.Queue[str] = queue.Queue()

        # Service handles
        self._irc: TwitchIRCClient | None = None
        self._tts: TTSEngine | None = None
        self._ai:  AIResponseHandler | None = None

        self._build_ui()
        self._start_services()
        self._poll_logs()

        self._log("[System] Ready.  Fill in the Connection tab and click Connect.")
        self._log_platform_info()

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
        self.grid_rowconfigure(0, weight=1)

        self._tabs = ctk.CTkTabview(self, anchor="nw")
        self._tabs.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        for name in ("Connection", "Twitch Plays", "AI Interaction", "Console"):
            self._tabs.add(name)

        self._build_connection(self._tabs.tab("Connection"))
        self._build_plays(self._tabs.tab("Twitch Plays"))
        self._build_ai(self._tabs.tab("AI Interaction"))
        self._build_console(self._tabs.tab("Console"))

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

        def file_field(label: str, attr: str, placeholder: str = "") -> None:
            nonlocal r
            ctk.CTkLabel(tab, text=label, anchor="e").grid(
                row=r, column=0, sticky="e", padx=(14, 8), pady=5)
            fr = ctk.CTkFrame(tab, fg_color="transparent")
            fr.grid(row=r, column=1, sticky="ew", padx=(0, 14), pady=5)
            fr.grid_columnconfigure(0, weight=1)
            e = ctk.CTkEntry(fr, placeholder_text=placeholder)
            e.grid(row=0, column=0, sticky="ew")
            ctk.CTkButton(
                fr, text="Browse", width=74,
                command=lambda _e=e: self._browse(_e),
            ).grid(row=0, column=1, padx=(6, 0))
            setattr(self, attr, e)
            r += 1

        # ── Twitch IRC ────────────────────────────────────────────────────────
        section("Twitch IRC")
        field("Channel",      "e_channel",  placeholder="channelname")
        field("Bot Username", "e_username", placeholder="mybotname")
        field("OAuth Token",  "e_token",    placeholder="oauth:xxxxxxxxxxxxxxxx", secret=True)

        # ── Local LLM ─────────────────────────────────────────────────────────
        section("Local LLM  (Ollama / LM Studio)")
        field(
            "API Endpoint", "e_endpoint",
            default="http://localhost:11434/v1/chat/completions",
            placeholder="http://localhost:11434/v1/chat/completions",
        )
        field("Model Name", "e_model", default="llama3", placeholder="llama3")

        # ── Piper TTS ─────────────────────────────────────────────────────────
        section("Piper TTS")
        file_field("Piper Executable",     "e_piper_exe",    "piper  or  /path/to/piper")
        file_field("Voice Model  (.onnx)", "e_piper_model",  "/path/to/voice.onnx")
        file_field("Model Config (.json)", "e_piper_cfg",    "/path/to/voice.onnx.json  (optional)")

        # ── Action buttons ────────────────────────────────────────────────────
        btn_row = ctk.CTkFrame(tab, fg_color="transparent")
        btn_row.grid(row=r, column=0, columnspan=2, padx=14, pady=18, sticky="w")

        self._btn_connect = ctk.CTkButton(
            btn_row, text="Connect", width=130,
            fg_color=_GREEN[0], hover_color=_GREEN[1],
            command=self._connect,
        )
        self._btn_connect.pack(side="left", padx=(0, 10))

        self._btn_disconnect = ctk.CTkButton(
            btn_row, text="Disconnect", width=130, state="disabled",
            fg_color=_RED[0], hover_color=_RED[1],
            command=self._disconnect,
        )
        self._btn_disconnect.pack(side="left", padx=(0, 10))

        self._lbl_conn_status = ctk.CTkLabel(
            btn_row, text="● Disconnected", text_color=OFF_FG,
            font=ctk.CTkFont(size=12),
        )
        self._lbl_conn_status.pack(side="left", padx=8)

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

        # Options
        opts = ctk.CTkFrame(tab, corner_radius=8)
        opts.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))
        opts.grid_columnconfigure(1, weight=1)

        def opt_row(label: str, widget_fn, row: int):
            ctk.CTkLabel(opts, text=label, anchor="e").grid(
                row=row, column=0, sticky="e", padx=(14, 8), pady=7)
            w = widget_fn()
            w.grid(row=row, column=1, sticky="w", padx=(0, 14), pady=7)
            return w

        self._e_every_n = opt_row(
            "Respond every N messages:",
            lambda: ctk.CTkEntry(opts, width=60),
            0,
        )
        self._e_every_n.insert(0, "5")

        self._var_mentions = ctk.BooleanVar(value=False)
        opt_row(
            "Respond to @bot mentions only:",
            lambda: ctk.CTkCheckBox(opts, text="", variable=self._var_mentions),
            1,
        )

        self._var_tts_ai = ctk.BooleanVar(value=True)
        opt_row(
            "Speak AI replies via TTS:",
            lambda: ctk.CTkCheckBox(opts, text="", variable=self._var_tts_ai),
            2,
        )

        # System prompt label + textbox
        ctk.CTkLabel(
            tab, text="System Prompt",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=2, column=0, sticky="w", padx=14, pady=(8, 2))

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

    # ── Console tab ───────────────────────────────────────────────────────────

    def _build_console(self, tab: ctk.CTkFrame) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        self._console = ctk.CTkTextbox(
            tab,
            state="disabled",
            font=ctk.CTkFont(family="Courier", size=12),
            text_color="#c8c8c8",
            wrap="word",
        )
        self._console.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 4))

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        ctk.CTkButton(bar, text="Clear Console", width=120,
                      command=self._clear_console).pack(side="left")
        ctk.CTkLabel(bar, text="Logs from all threads appear here.",
                     text_color="gray").pack(side="left", padx=14)

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

    # ── Config getters (called on worker threads; simple .get() reads are
    #    safe under CPython's GIL for non-mutating reads) ─────────────────────

    def _get_tts_cfg(self) -> dict:
        return {
            "piper_exe":   self.e_piper_exe.get().strip()   or "piper",
            "model_path":  self.e_piper_model.get().strip(),
            "config_path": self.e_piper_cfg.get().strip(),
        }

    def _get_ai_cfg(self) -> dict:
        return {
            "endpoint":      self.e_endpoint.get().strip(),
            "model":         self.e_model.get().strip(),
            "system_prompt": self._system_prompt.get("1.0", "end-1c"),
            "tts_ai":        self._var_tts_ai.get(),
        }

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

    # ══════════════════════════════════════════════════════════════════════════
    # Message dispatch  (called from IRC thread)
    # ══════════════════════════════════════════════════════════════════════════

    def _dispatch(self, username: str, message: str) -> None:
        """
        Central dispatcher — routes each PRIVMSG to the Plays system and/or
        the AI system.  Runs on the IRC background thread; must never touch
        Tkinter widgets directly.  Use _log() which is queue-safe.
        """
        self._log(f"[Chat] {username}: {message}")
        self._route_plays(username, message)
        self._route_ai(username, message)

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

    def _route_ai(self, username: str, message: str) -> None:
        if not self.ai_enabled.get():
            return

        if self._var_mentions.get():
            bot = self.e_username.get().lower()
            if bot and bot not in message.lower():
                return
            self._ai.handle(username, message)
            return

        try:
            every_n = max(1, int(self._e_every_n.get()))
        except ValueError:
            every_n = 5

        self._ai_counter += 1
        if self._ai_counter >= every_n:
            self._ai_counter = 0
            self._ai.handle(username, message)

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

        self.after(80, self._poll_logs)

    def _clear_console(self) -> None:
        self._console.configure(state="normal")
        self._console.delete("1.0", "end")
        self._console.configure(state="disabled")

    # ══════════════════════════════════════════════════════════════════════════
    # Utilities
    # ══════════════════════════════════════════════════════════════════════════

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
