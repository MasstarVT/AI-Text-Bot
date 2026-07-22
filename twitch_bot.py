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
  TTS thread    │ TTSEngine._worker() — persistent Piper process + _reader thread
                │   _worker writes JSON lines to Piper stdin; _reader parses RIFF
                │   WAV frames from stdout and broadcasts base64 audio via SSE
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
import base64
import collections
import json
import os
import random
import re
import queue
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

import flask as _flask
import requests

# ── Optional dependencies (graceful degradation) ────────────────────────────
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

_DEFAULT_THANKS_PROMPT = (
    "You are a friendly Twitch streamer's bot. When a viewer follows, subs, resubs, gifts subs, "
    "cheers bits, or raids, respond with a warm, brief, personalized thank-you message "
    "that fits naturally in Twitch chat. Keep it under two sentences. Do not use hashtags."
)
_THANKS_TEMPLATES: dict[str, Callable[[str, dict], str]] = {
    "follow":      lambda u, e: f"[EVENT] {u} just followed! Welcome them to the community.",
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

_PLACEHOLDER_RE = re.compile(r"%[a-zA-Z0-9_]+(?::[^%]+)?%")


def _calc_uptime(started_at: str) -> str:
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - start
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m = rem // 60
        return f"{h}h {m}m" if h else f"{m}m"
    except Exception:
        return "offline"


_DATA_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


def _safe_data_path(data_dir: str, name: str) -> str | None:
    if not data_dir:
        return None
    if not _DATA_NAME_RE.fullmatch(name):
        return None
    if ".." in name or name == ".":
        return None
    return os.path.join(data_dir, name)


def _file_counter(path: str) -> str:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as f:
                val = int(f.read().strip())
        except FileNotFoundError:
            val = 0
        except ValueError:
            return "(invalid counter)"
        val += 1
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(val))
        os.replace(tmp, path)
        return str(val)
    except Exception:
        return "(error)"


def _file_random_line(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln.rstrip() for ln in f if ln.strip()]
        return random.choice(lines) if lines else "(empty file)"
    except FileNotFoundError:
        return "(file not found)"
    except Exception:
        return "(error)"


def _file_line(path: str, n: int) -> str:
    if n < 1:
        return "(invalid line)"
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if n > len(lines):
            return "(line not found)"
        return lines[n - 1].rstrip()
    except FileNotFoundError:
        return "(file not found)"
    except Exception:
        return "(error)"


def _apply_placeholders(
    response: str,
    username: str,
    channel: str,
    command: str,
    args: str,
    cmd_count: int = 0,
    stream_info: dict | None = None,
    data_dir: str = "",
) -> str:
    if stream_info is None:
        stream_info = {}
    now = datetime.now()
    touser = args.split()[0].lstrip("@") if args.split() else ""
    static: dict[str, str] = {
        "%user%":    username,
        "%channel%": channel,
        "%command%": command,
        "%args%":    args,
        "%touser%":  touser,
        "%time%":    now.strftime("%H:%M"),
        "%date%":    now.strftime("%B ") + str(now.day) + now.strftime(", %Y"),
        "%count%":   str(cmd_count),
        "%game%":    stream_info.get("game_name", "offline"),
        "%title%":   stream_info.get("title", "offline"),
        "%viewers%": str(stream_info["viewer_count"]) if "viewer_count" in stream_info else "offline",
        "%uptime%":  _calc_uptime(stream_info["started_at"]) if "started_at" in stream_info else "offline",
    }

    def _replace(m: re.Match) -> str:
        token = m.group(0)
        if token in static:
            return static[token]
        if token == "%random%":
            return str(random.randint(1, 100))
        rm = re.fullmatch(r"%random:(\d+)-(\d+)%", token)
        if rm:
            lo, hi = int(rm.group(1)), int(rm.group(2))
            return str(random.randint(lo, hi)) if lo <= hi else token
        cm = re.fullmatch(r"%counter:([^%]+)%", token)
        if cm:
            path = _safe_data_path(data_dir, cm.group(1))
            return _file_counter(path) if path else token
        rl = re.fullmatch(r"%randomline:([^%]+)%", token)
        if rl:
            path = _safe_data_path(data_dir, rl.group(1))
            return _file_random_line(path) if path else token
        lm = re.fullmatch(r"%line:(\d+):([^%]+)%", token)
        if lm:
            path = _safe_data_path(data_dir, lm.group(2))
            return _file_line(path, int(lm.group(1))) if path else token
        return token

    return _PLACEHOLDER_RE.sub(_replace, response)


def _scan_voices_dir(voices_dir: str) -> list[str]:
    """Return sorted .onnx voice names (without extension) from voices_dir."""
    try:
        return sorted(
            f[:-5] for f in os.listdir(voices_dir)
            if f.endswith(".onnx")
        )
    except FileNotFoundError:
        return []


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
            except Exception:
                pass  # keyDown failed — fall through to pynput
            else:
                try:
                    time.sleep(duration)
                finally:
                    try:
                        pydirectinput.keyUp(key)
                    except Exception:
                        pass
                return

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
    Text-to-Speech via Piper TTS (persistent subprocess).

    speak(text) enqueues work; a single daemon _worker thread writes JSON
    lines to a persistent Piper process kept alive across clips.  A _reader
    daemon thread parses WAV frames from Piper's stdout (RIFF framing) and
    forwards each frame to the on_audio callback for SSE delivery.

    Model changes are detected on the next speak() call; Piper is restarted
    automatically with the new model path.
    """

    def __init__(self, get_config, log, on_audio=None) -> None:
        self.get_config = get_config   # callable → dict(piper_exe, model_path, config_path)
        self.log = log
        self.on_audio = on_audio          # callable(wav_b64: str) | None
        self._q: queue.Queue[str | None] = queue.Queue()
        self._stop_event = threading.Event()
        self._piper_proc: subprocess.Popen | None = None
        self._piper_lock = threading.Lock()
        self._current_model: str = ""
        self._launch_piper(self.get_config())
        threading.Thread(target=self._worker, name="TTS-Worker", daemon=True).start()

    def speak(self, text: str) -> None:
        self._q.put(text)

    def stop(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._q.put(None)
        self._kill_piper()

    def panic(self) -> None:
        """Drain the queue and signal the reader thread to discard the current frame."""
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
        self._stop_event.set()

    # ── internal ─────────────────────────────────────────────────────────────

    def _kill_piper(self) -> None:
        with self._piper_lock:
            proc = self._piper_proc
            self._piper_proc = None
        if proc:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass

    def _launch_piper(self, cfg: dict) -> None:
        piper_exe  = cfg.get("piper_exe")   or "piper"
        model_path = cfg.get("model_path")  or ""
        cfg_path   = cfg.get("config_path") or ""

        if not model_path:
            return

        # Kill old process before starting a new one
        with self._piper_lock:
            old_proc = self._piper_proc
            self._piper_proc = None
        if old_proc:
            try:
                old_proc.stdin.close()
            except Exception:
                pass
            try:
                old_proc.kill()
            except Exception:
                pass

        cmd = [piper_exe, "--model", model_path, "--json-input", "--output_file", "-"]
        if cfg_path:
            cmd += ["--config", cfg_path]

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with self._piper_lock:
                self._piper_proc = proc
                self._current_model = model_path
            threading.Thread(target=self._reader, args=(proc,),
                             name="TTS-Reader", daemon=True).start()
            threading.Thread(
                target=lambda: proc.stderr.read(),
                name="TTS-Stderr-Drain", daemon=True,
            ).start()
        except FileNotFoundError:
            self.log(f"[TTS] Piper executable not found: '{piper_exe}'")
        except Exception as exc:
            self.log(f"[TTS] Failed to start Piper: {exc}")

    def _reader(self, proc: subprocess.Popen) -> None:
        """Parse WAV frames from Piper stdout; discard frames when _stop_event is set."""
        try:
            while True:
                header = self._read_exactly(proc.stdout, 8)
                if header is None or header[:4] != b'RIFF':
                    break
                chunk_size = int.from_bytes(header[4:8], 'little')
                rest = self._read_exactly(proc.stdout, chunk_size)
                if rest is None:
                    break
                if self._stop_event.is_set():
                    self._stop_event.clear()
                    continue
                if self.on_audio:
                    wav_b64 = base64.b64encode(header + rest).decode("ascii")
                    self.on_audio(wav_b64)
        except Exception as exc:
            with self._piper_lock:
                if self._piper_proc is proc:
                    self.log(f"[TTS] Reader error: {exc}")
        finally:
            with self._piper_lock:
                if self._piper_proc is proc:
                    self._piper_proc = None

    @staticmethod
    def _read_exactly(f, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = f.read(n - len(buf))
            except Exception:
                return None
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    # ── worker ───────────────────────────────────────────────────────────────

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            if self._stop_event.is_set():
                self._stop_event.clear()
                continue
            try:
                self._synthesize(item)
            except Exception as exc:
                self.log(f"[TTS] Synthesize error: {exc}")

    def _synthesize(self, text: str) -> None:
        cfg        = self.get_config()
        model_path = cfg.get("model_path") or ""

        if not model_path:
            self.log("[TTS] No voice model configured — skipping speech.")
            return

        with self._piper_lock:
            needs_restart = (model_path != self._current_model)

        if needs_restart:
            self._launch_piper(cfg)

        with self._piper_lock:
            proc = self._piper_proc

        if proc is None:
            self._launch_piper(cfg)
            with self._piper_lock:
                proc = self._piper_proc
            if proc is None:
                return

        try:
            line = json.dumps({"text": text}).encode() + b'\n'
            proc.stdin.write(line)
            proc.stdin.flush()
        except OSError as exc:
            self.log(f"[TTS] Write to Piper failed: {exc}")


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

    def __init__(self, get_config, log, tts: TTSEngine, on_thinking=None) -> None:
        self.get_config   = get_config   # callable → dict(endpoint, model, system_prompt, tts_ai)
        self.log          = log
        self.tts          = tts
        self._on_thinking = on_thinking  # callable(bool) | None
        self._q: queue.Queue[tuple | None] = queue.Queue()
        threading.Thread(target=self._worker, name="AI-Worker", daemon=True).start()

    def handle(self, username: str, message: str, reply_cb=None,
               prompt_override: str | None = None, use_tts: bool | None = None,
               context: list | None = None) -> None:
        self._q.put((username, message, reply_cb, prompt_override, use_tts, context))

    def stop(self) -> None:
        self._q.put(None)

    @staticmethod
    def _is_sentence_boundary(text: str) -> bool:
        """True if text ends at a sentence boundary suitable for TTS dispatch."""
        stripped = text.rstrip()
        return len(stripped) >= 8 and stripped[-1] in '.!?'

    def _stream_openai(self, endpoint: str, model: str, api_key: str,
                       system_prompt: str, user_content: str,
                       tts_cb) -> str:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_content},
            ],
            "stream": True,
            "max_tokens": 1500,
        }
        resp = requests.post(endpoint, headers=headers, json=payload,
                             timeout=90, stream=True)
        resp.raise_for_status()

        full_tokens: list[str] = []
        sentence_buf: list[str] = []

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode("utf-8", errors="replace")
            if not raw_line.startswith("data: "):
                continue
            data = raw_line[6:]
            if data.strip() == "[DONE]":
                break
            try:
                chunk  = json.loads(data)
                token  = chunk["choices"][0]["delta"].get("content") or ""
                finish = chunk["choices"][0].get("finish_reason") or ""
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            if finish == "length":
                self.log("[AI] Ran out of tokens mid-think — try a shorter system prompt or a non-reasoning model.")
            if not token:
                continue
            full_tokens.append(token)
            sentence_buf.append(token)
            buf_str = "".join(sentence_buf)
            if self._is_sentence_boundary(buf_str) and tts_cb:
                tts_cb(buf_str.strip())
                sentence_buf = []

        remainder = "".join(sentence_buf).strip()
        if remainder and tts_cb:
            tts_cb(remainder)

        return "".join(full_tokens).strip()

    def _stream_anthropic(self, endpoint: str, model: str, api_key: str,
                          system_prompt: str, user_content: str,
                          tts_cb) -> str:
        headers = {
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }
        payload = {
            "model":      model,
            "max_tokens": 1500,
            "stream":     True,
            "system":     system_prompt,
            "messages":   [{"role": "user", "content": user_content}],
        }
        resp = requests.post(endpoint, headers=headers, json=payload,
                             timeout=90, stream=True)
        resp.raise_for_status()

        full_tokens: list[str] = []
        sentence_buf: list[str] = []

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode("utf-8", errors="replace")
            if not raw_line.startswith("data: "):
                continue
            try:
                event = json.loads(raw_line[6:])
                if event.get("type") == "message_delta":
                    if event.get("delta", {}).get("stop_reason") == "max_tokens":
                        self.log("[AI] Ran out of tokens mid-think — try a shorter system prompt or a non-reasoning model.")
                    continue
                if event.get("type") != "content_block_delta":
                    continue
                token = event.get("delta", {}).get("text") or ""
            except (json.JSONDecodeError, KeyError):
                continue
            if not token:
                continue
            full_tokens.append(token)
            sentence_buf.append(token)
            buf_str = "".join(sentence_buf)
            if self._is_sentence_boundary(buf_str) and tts_cb:
                tts_cb(buf_str.strip())
                sentence_buf = []

        remainder = "".join(sentence_buf).strip()
        if remainder and tts_cb:
            tts_cb(remainder)

        return "".join(full_tokens).strip()

    # ── worker ───────────────────────────────────────────────────────────────

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            username, message, reply_cb, prompt_override, use_tts, context = item
            if self._on_thinking:
                self._on_thinking(True)
            try:
                self._query(username, message, reply_cb=reply_cb,
                            prompt_override=prompt_override, use_tts=use_tts,
                            context=context)
            finally:
                if self._on_thinking:
                    self._on_thinking(False)

    def _query(self, username: str, message: str, reply_cb=None,
               prompt_override: str | None = None, use_tts: bool | None = None,
               context: list | None = None) -> None:
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

        if context:
            ctx_lines    = "\n".join(f"{u}: {m}" for u, m in context)
            user_content = f"[Recent chat]\n{ctx_lines}\n\n{username}: {message}"
        else:
            user_content = f"{username}: {message}"

        try:
            tts_cb = self.tts.speak if _use_tts else None
            if fmt == "anthropic":
                reply = self._stream_anthropic(endpoint, model, api_key, system_prompt, user_content, tts_cb)
            else:
                reply = self._stream_openai(endpoint, model, api_key, system_prompt, user_content, tts_cb)

            if not reply:
                self.log("[AI] Model returned an empty response.")
                return
            self.log(f"[AI] → {reply}")
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

    def __init__(self, get_creds, log, on_message, on_ready=None, on_reconnecting=None, on_event=None) -> None:
        self.get_creds       = get_creds       # callable → dict(channel, username, token)
        self.log             = log
        self.on_message      = on_message      # callback(username, message, bits, reward_id, badges)
        self.on_ready        = on_ready        # called once when JOIN is confirmed
        self.on_reconnecting = on_reconnecting # called on unexpected disconnect (before backoff sleep)
        self.on_event: callable | None = on_event  # callback(event_type: str, username: str, extra: dict)
        self._sock: socket.socket | None = None
        self._running = False
        self._ready_fired = False

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
        text = text.replace("\r", " ").replace("\n", " ")  # prevent CRLF injection
        sock = self._sock  # snapshot to avoid TOCTOU with disconnect()
        if sock:
            try:
                sock.sendall(f"PRIVMSG #{channel} :{text}\r\n".encode())
            except OSError as exc:
                self.log(f"[IRC] Send failed: {exc}")

    # ── reconnect loop ────────────────────────────────────────────────────────

    def _run(self) -> None:
        delay = 1
        while self._running:
            try:
                self._session()
                delay = 1  # reset backoff after a clean session
            except Exception as exc:
                if self._running:
                    self.log(f"[IRC] Disconnected ({exc}). Reconnecting in {delay}s…")
                    if self.on_reconnecting:
                        self.on_reconnecting()
                    time.sleep(delay)
                    delay = min(delay * 2, 30)

    def _session(self) -> None:
        self._ready_fired = False
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

        if not self._ready_fired and " JOIN #" in line:
            self._ready_fired = True
            if self.on_ready:
                self.on_ready()

        m = re.search(r":(\w+)!\w+@\S+\.tmi\.twitch\.tv PRIVMSG #\S+ :(.+)", line)
        if m:
            bits      = int(tags.get("bits", 0) or 0)
            reward_id = tags.get("custom-reward-id", "")
            badges    = tags.get("badges", "")
            self.on_message(m.group(1), m.group(2).strip(), bits, reward_id, badges)

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


# ══════════════════════════════════════════════════════════════════════════════
# EventSubClient
# ══════════════════════════════════════════════════════════════════════════════
class EventSubClient:
    """
    Subscribes to Twitch EventSub `channel.follow` via WebSocket transport.

    Requires the OAuth token to have the `moderator:read:followers` scope and a
    valid Client ID from a registered Twitch application (dev.twitch.tv).

    Runs on a dedicated daemon thread with its own asyncio event loop, mirroring
    the DiscordClient pattern.
    """

    _WS_URL      = "wss://eventsub.wstv.twitch.tv/ws"
    _HELIX_USERS = "https://api.twitch.tv/helix/users"
    _HELIX_SUB   = "https://api.twitch.tv/helix/eventsub/subscriptions"

    def __init__(self, get_creds, log, on_event) -> None:
        self._get_creds = get_creds  # callable → dict(channel, token, client_id)
        self._log       = log
        self._on_event  = on_event   # callback(event_type: str, username: str, extra: dict)
        self._thread: threading.Thread | None = None
        self._loop:   asyncio.AbstractEventLoop | None = None
        self._stop    = threading.Event()

    def connect(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="EventSub")
        self._thread.start()

    def disconnect(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._session())
        except Exception as exc:
            self._log(f"[EventSub] Fatal error: {exc}")
        finally:
            self._loop.close()

    async def _session(self) -> None:
        creds     = self._get_creds()
        raw_token = creds.get("token", "")
        token     = raw_token.removeprefix("oauth:")
        client_id = creds.get("client_id", "")
        channel   = creds.get("channel", "").lower().strip()

        if not token or not client_id or not channel:
            self._log("[EventSub] Skipping follow events — token, client_id, or channel missing")
            return

        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id":     client_id,
            "Content-Type":  "application/json",
        }

        # Resolve broadcaster user ID once
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(self._HELIX_USERS, params={"login": channel},
                                    headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
            users = data.get("data", [])
            if not users:
                self._log(f"[EventSub] Could not resolve user ID for channel '{channel}'")
                return
            broadcaster_id = users[0]["id"]
        except Exception as exc:
            self._log(f"[EventSub] User lookup failed: {exc}")
            return

        ws_url   = self._WS_URL
        attempts = 0

        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.ws_connect(ws_url,
                                               heartbeat=25,
                                               timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                        attempts = 0
                        reconnect_to: str | None = None
                        async for msg in ws:
                            if self._stop.is_set():
                                return
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                reconnect_to = await self._handle_msg(
                                    json.loads(msg.data), http, headers, broadcaster_id
                                )
                                if reconnect_to:
                                    break
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                        if reconnect_to:
                            ws_url = reconnect_to
                            continue
            except Exception as exc:
                if self._stop.is_set():
                    return
                attempts += 1
                delay = min(2 ** attempts, 120)
                self._log(f"[EventSub] Disconnected ({exc}), reconnecting in {delay}s…")
                await asyncio.sleep(delay)

    async def _handle_msg(self, payload: dict, http, headers: dict,
                          broadcaster_id: str) -> str | None:
        """Returns a reconnect URL if session_reconnect received, else None."""
        msg_type = payload.get("metadata", {}).get("message_type", "")

        if msg_type == "session_welcome":
            session_id = payload["payload"]["session"]["id"]
            self._log("[EventSub] Connected — subscribing to channel.follow")
            await self._subscribe(http, headers, broadcaster_id, session_id)

        elif msg_type == "notification":
            p    = payload.get("payload", {})
            kind = p.get("subscription", {}).get("type", "")
            if kind == "channel.follow":
                username = p.get("event", {}).get("user_name", "")
                if username:
                    self._on_event("follow", username, {})

        elif msg_type == "session_reconnect":
            return payload["payload"]["session"].get("reconnect_url", self._WS_URL)

        return None

    async def _subscribe(self, http, headers: dict,
                         broadcaster_id: str, session_id: str) -> None:
        body = {
            "type":    "channel.follow",
            "version": "2",
            "condition": {
                "broadcaster_user_id": broadcaster_id,
                "moderator_user_id":   broadcaster_id,
            },
            "transport": {"method": "websocket", "session_id": session_id},
        }
        try:
            async with http.post(self._HELIX_SUB, json=body,
                                 headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 202:
                    self._log("[EventSub] Subscribed — follow events active")
                else:
                    resp_body = await r.json()
                    msg = resp_body.get("message", str(r.status))
                    if r.status == 403:
                        self._log(
                            f"[EventSub] Subscription failed (403): {msg} — "
                            "token needs 'moderator:read:followers' scope. "
                            "Generate a new token at https://twitchtokengenerator.com "
                            "and add that scope."
                        )
                    else:
                        self._log(f"[EventSub] Subscription failed ({r.status}): {msg}")
        except Exception as exc:
            self._log(f"[EventSub] Subscription request error: {exc}")


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
        # ── thank-you responses ────────────────────────────────────────────────
        "thanks_enabled": False,
        "thanks_follow":  True,
        "thanks_sub":     True,
        "thanks_resub":   True,
        "thanks_gift":    True,
        "thanks_mystery": True,
        "thanks_bits":    False,
        "thanks_raid":    True,
        "thanks_chat":              True,
        "thanks_tts":               True,
        "thanks_use_shared_prompt": False,
        "thanks_prompt":            "",
        "thanks_cooldown_enabled": False,
        "thanks_cooldown_secs":    30,
        # ── ignore list ────────────────────────────────────────────────────────
        "ignore_list_enabled": False,
        "ignore_list":         [],
        # ── chat commands ──────────────────────────────────────────────────────
        "chat_commands_enabled": False,
        "chat_commands":         {},
        "cmd_list_enabled":      False,
        # ── scheduled messages ─────────────────────────────────────────────────
        "scheduled_msgs": [],
        # ── chat context ───────────────────────────────────────────────────────
        "ai_context_enabled": False,
        "ai_context_size":    5,
        # ── quotes ─────────────────────────────────────────────────────────────
        "quote_addquote_role": "moderator",
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
            "bot_username":            env.get("BOT_USERNAME", "") or env.get("TWITCH_USERNAME", ""),
            "bot_token":               env.get("BOT_TOKEN",    "") or env.get("TWITCH_TOKEN",    ""),
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
            "web_token":               env.get("WEB_TOKEN", ""),
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
            "thanks_enabled":   settings.get("thanks_enabled",  False),
            "thanks_follow":    settings.get("thanks_follow",    True),
            "thanks_sub":       settings.get("thanks_sub",       True),
            "thanks_resub":     settings.get("thanks_resub",     True),
            "thanks_gift":      settings.get("thanks_gift",      True),
            "thanks_mystery":   settings.get("thanks_mystery",   True),
            "thanks_bits":      settings.get("thanks_bits",      False),
            "thanks_raid":      settings.get("thanks_raid",      True),
            "thanks_chat":              settings.get("thanks_chat",              True),
            "thanks_tts":               settings.get("thanks_tts",               True),
            "thanks_use_shared_prompt": settings.get("thanks_use_shared_prompt", False),
            "thanks_prompt":            settings.get("thanks_prompt",            ""),
            "thanks_cooldown_enabled": settings.get("thanks_cooldown_enabled", False),
            "thanks_cooldown_secs":    int(settings.get("thanks_cooldown_secs", 30)),
            "ignore_list_enabled": settings.get("ignore_list_enabled", False),
            "ignore_list":         [str(u).lower().strip() for u in settings.get("ignore_list", []) if u],
            "chat_commands_enabled": settings.get("chat_commands_enabled", False),
            "chat_commands": {
                k: (
                    {**v, "allowed_roles": v.get("allowed_roles", [])}
                    if isinstance(v, dict)
                    else {"response": v, "cooldown": 0, "cooldown_type": "global", "allowed_roles": []}
                )
                for k, v in settings.get("chat_commands", {}).items()
            },
            "cmd_list_enabled": settings.get("cmd_list_enabled", False),
            "scheduled_msgs": list(settings.get("scheduled_msgs", [])),
            "ai_context_enabled": settings.get("ai_context_enabled", False),
            "ai_context_size":    int(settings.get("ai_context_size", 5)),
            "quote_addquote_role": settings.get("quote_addquote_role", "moderator"),
            "system_prompt":    "",
            # ── connection status ───────────────────────────────────────────
            "twitch_status":    "off",   # off / connecting / online
            "discord_status":   "off",   # off / connecting / online / error
        }
        self._config_lock = threading.Lock()
        self._ai_counter      = 0
        self._ai_counter_lock = threading.Lock()
        self._chat_history: collections.deque[tuple[str, str]] = collections.deque(maxlen=20)
        self._history_lock  = threading.Lock()
        self._last_thanks_time: float = 0.0
        self._thanks_lock = threading.Lock()
        self._cmd_global_cooldowns: dict[str, float]             = {}
        self._cmd_user_cooldowns:   dict[tuple[str, str], float] = {}
        self._cmd_use_counts:   dict[str, int]  = {}
        self._cmd_cooldowns_lock = threading.Lock()
        self._data_dir          = os.path.join(_here, "data")
        self._stream_cache:     dict             = {}
        self._stream_cache_ts:  float            = 0.0
        self._stream_cache_lock = threading.Lock()
        self._roles_lock    = threading.Lock()
        self._counters_lock = threading.Lock()
        self._quotes_lock   = threading.Lock()

        # Generate a persistent web API token if not already set
        if not self._config.get("web_token"):
            import secrets
            self._config["web_token"] = secrets.token_hex(16)

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
        self._irc:      TwitchIRCClient | None   = None
        self._tts:      TTSEngine | None         = None
        self._ai:       AIResponseHandler | None = None
        self._discord:  DiscordClient | None     = None
        self._eventsub: EventSubClient | None    = None

        # Flask app
        self._flask = _flask.Flask(
            __name__,
            template_folder=os.path.join(_here, "templates"),
        )
        self._register_routes()

        self._init_counter_presets()
        self._start_services()
        self._log("[System] Ready.")
        self._log_platform_info()
        self._autosave()

        _sched = threading.Thread(target=self._scheduler_loop, name="Scheduler", daemon=True)
        _sched.start()

        # Auto-connect if credentials already saved
        if all(self._config.get(k) for k in
               ("twitch_channel", "bot_username", "bot_token")):
            t = threading.Timer(0.8, self._connect)
            t.daemon = True
            t.start()
        if self._config.get("discord_token") and self._config.get("discord_channel_id"):
            t = threading.Timer(1.2, self._discord_connect)
            t.daemon = True
            t.start()

    def _log_platform_info(self) -> None:
        libs = []
        libs.append("pydirectinput ✓" if HAS_PYDIRECTINPUT else "pydirectinput ✗")
        libs.append("pynput ✓" if HAS_PYNPUT else "pynput ✗")
        self._log(f"[System] Libraries: {', '.join(libs)}")
        if not HAS_PYDIRECTINPUT and not HAS_PYNPUT:
            self._log("[System] WARNING: No input library found — Twitch Plays disabled.")

    def _scheduler_loop(self) -> None:
        last_fired: dict[tuple[str, int], float] = {}
        while True:
            time.sleep(30)
            try:
                with self._config_lock:
                    msgs    = list(self._config.get("scheduled_msgs", []))
                    online  = self._config.get("twitch_status") == "online"
                    channel = self._config.get("twitch_channel", "").lower().strip()
                if not online or not channel or not msgs:
                    continue
                now = time.time()
                active_keys: set[tuple[str, int]] = set()
                for entry in msgs:
                    text     = entry.get("text", "").strip()
                    interval = max(1, int(entry.get("interval", 30))) * 60
                    if not text:
                        continue
                    key = (text, interval)
                    active_keys.add(key)
                    if now - last_fired.get(key, 0) >= interval:
                        last_fired[key] = now
                        irc = self._irc
                        if irc:
                            irc.say(channel, text[:500])
                            self._log(f"[Scheduled] → {text}")
                last_fired = {k: v for k, v in last_fired.items() if k in active_keys}
            except Exception as exc:
                self._log(f"[Scheduler] Error: {exc}")

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

    def _broadcast_tts_audio(self, wav_b64: str) -> None:
        """Push a TTS audio clip to all SSE clients."""
        msg = f"event: tts\ndata: {json.dumps({'wav': wav_b64})}\n\n"
        with self._log_lock:
            for q in list(self._sse_clients):
                q.put(msg)

    def _broadcast_ai_thinking(self, thinking: bool) -> None:
        event = "ai-thinking" if thinking else "ai-done"
        msg = f"event: {event}\ndata: {{}}\n\n"
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
                "channel":  self._config.get("twitch_channel", ""),
                "username": self._config.get("bot_username",   ""),
                "token":    self._config.get("bot_token",      ""),
            }

    def _get_eventsub_creds(self) -> dict:
        with self._config_lock:
            return {
                "channel":   self._config.get("twitch_channel",   ""),
                "token":     self._config.get("twitch_token",     ""),
                "client_id": self._config.get("twitch_client_id", ""),
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
            f"BOT_USERNAME={c.get('bot_username', '')}",
            f"BOT_TOKEN={c.get('bot_token', '')}",
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
            f"WEB_TOKEN={c.get('web_token', '')}",
        ]
        tmp = self._env_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, self._env_path)

    def _load_settings(self) -> dict:
        s = dict(self._SETTINGS_DEFAULTS)
        if os.path.exists(self._settings_path):
            try:
                with open(self._settings_path, encoding="utf-8") as f:
                    s.update(json.load(f))
            except Exception:
                pass
        # Coerce int fields — old settings.json may store them as strings
        _INT_FIELDS = {"every_n", "min_bits", "thanks_cooldown_secs", "ai_context_size"}
        for key in _INT_FIELDS:
            if key in s:
                try:
                    s[key] = int(s[key])
                except (TypeError, ValueError):
                    s[key] = self._SETTINGS_DEFAULTS.get(key, 0)
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
            # ── thank-you responses ────────────────────────────────────────
            "thanks_enabled":   c.get("thanks_enabled",   False),
            "thanks_follow":    c.get("thanks_follow",    True),
            "thanks_sub":       c.get("thanks_sub",       True),
            "thanks_resub":     c.get("thanks_resub",     True),
            "thanks_gift":      c.get("thanks_gift",      True),
            "thanks_mystery":   c.get("thanks_mystery",   True),
            "thanks_bits":      c.get("thanks_bits",      False),
            "thanks_raid":      c.get("thanks_raid",      True),
            "thanks_chat":              c.get("thanks_chat",              True),
            "thanks_tts":               c.get("thanks_tts",               True),
            "thanks_use_shared_prompt": c.get("thanks_use_shared_prompt", False),
            "thanks_prompt":            c.get("thanks_prompt",            ""),
            "thanks_cooldown_enabled": c.get("thanks_cooldown_enabled", False),
            "thanks_cooldown_secs":    c.get("thanks_cooldown_secs",    30),
            # ── ignore list ────────────────────────────────────────────────────
            "ignore_list_enabled": c.get("ignore_list_enabled", False),
            "ignore_list":         c.get("ignore_list",         []),
            "chat_commands_enabled": c.get("chat_commands_enabled", False),
            "chat_commands":         c.get("chat_commands",         {}),
            "cmd_list_enabled":      c.get("cmd_list_enabled",      False),
            "scheduled_msgs": c.get("scheduled_msgs", []),
            "ai_context_enabled": c.get("ai_context_enabled", False),
            "ai_context_size":    c.get("ai_context_size",    5),
            "quote_addquote_role": c.get("quote_addquote_role", "moderator"),
        }
        tmp = self._settings_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self._settings_path)

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

        @app.before_request
        def _check_api_token():
            if _flask.request.path.startswith("/api/"):
                expected = self._config.get("web_token", "")
                if not expected:
                    return  # no token yet (first startup before any connect)
                if _flask.request.headers.get("X-Bot-Token") != expected:
                    return _flask.jsonify({"error": "unauthorized"}), 401

        # ── page ──────────────────────────────────────────────────────────────

        @app.route("/")
        def index():
            token = self._config.get("web_token", "")
            return _flask.render_template("index.html", web_token=token)

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
                               "twitch_client_id", "twitch_token",
                               "bot_username", "bot_token"):
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
            msg = "event: tts-panic\ndata: {}\n\n"
            with self._log_lock:
                for q in list(self._sse_clients):
                    q.put(msg)
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
            "bot_username", "bot_token",
            "llm_provider", "llm_endpoint", "llm_model", "llm_api_key",
            "piper_exe", "piper_model", "piper_config",
            "discord_token", "discord_channel_id", "discord_trigger",
            "discord_use_shared_prompt", "discord_prompt",
            "trigger_every_n", "every_n", "trigger_mentions", "trigger_bits",
            "min_bits", "trigger_points", "reward_id", "tts_ai",
            # ── thank-you responses ──────────────────────────────────────────
            "thanks_enabled", "thanks_follow", "thanks_sub", "thanks_resub", "thanks_gift",
            "thanks_mystery", "thanks_bits", "thanks_raid", "thanks_chat", "thanks_tts",
            "thanks_use_shared_prompt", "thanks_prompt",
            "thanks_cooldown_enabled", "thanks_cooldown_secs",
            "ignore_list_enabled", "ignore_list",
            "chat_commands_enabled", "chat_commands",
            "cmd_list_enabled",
            "scheduled_msgs",
            "ai_context_enabled", "ai_context_size",
            "quote_addquote_role",
        )

        @app.route("/api/settings", methods=["GET"])
        def api_settings_get():
            with self._config_lock:
                c = dict(self._config)
            return _flask.jsonify({k: c.get(k) for k in _SETTINGS_KEYS})

        @app.route("/api/settings", methods=["POST"])
        def api_settings_post():
            data = _flask.request.get_json(force=True, silent=True) or {}
            _INT_KEYS = {"every_n", "min_bits", "thanks_cooldown_secs", "ai_context_size"}
            _BOOL_KEYS = {
                "trigger_every_n", "trigger_mentions", "trigger_bits",
                "trigger_points", "tts_ai", "discord_use_shared_prompt",
                "thanks_enabled", "thanks_follow", "thanks_sub", "thanks_resub", "thanks_gift",
                "thanks_mystery", "thanks_bits", "thanks_raid", "thanks_chat", "thanks_tts",
                "thanks_use_shared_prompt",
                "thanks_cooldown_enabled",
                "ignore_list_enabled",
                "chat_commands_enabled",
                "cmd_list_enabled",
                "ai_context_enabled",
            }
            with self._config_lock:
                for k in _SETTINGS_KEYS:
                    if k in data:
                        if k == "ignore_list":
                            if isinstance(data[k], list):
                                self._config[k] = [str(u).lower().strip() for u in data[k] if u]
                        elif k == "chat_commands":
                            if isinstance(data[k], dict):
                                cmds = {}
                                for cmd, entry in data[k].items():
                                    cmd = str(cmd).lower().strip()
                                    if not cmd:
                                        continue
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
                                        raw_roles = entry.get("allowed_roles", [])
                                        if isinstance(raw_roles, list):
                                            allowed_roles = [str(r).strip().lower() for r in raw_roles if r]
                                        elif isinstance(raw_roles, str) and raw_roles.strip():
                                            allowed_roles = [r.strip().lower() for r in raw_roles.split(",") if r.strip()]
                                        else:
                                            allowed_roles = []
                                        cmds[cmd] = {"response": response, "cooldown": cooldown,
                                                     "cooldown_type": cooldown_type, "allowed_roles": allowed_roles}
                                    else:
                                        resp = str(entry).strip()
                                        if cmd and resp:
                                            cmds[cmd] = {"response": resp, "cooldown": 0,
                                                         "cooldown_type": "global", "allowed_roles": []}
                                self._config[k] = cmds
                        elif k == "scheduled_msgs":
                            if isinstance(data[k], list):
                                msgs = []
                                for e in data[k]:
                                    if not isinstance(e, dict):
                                        continue
                                    text     = str(e.get("text", "")).strip()
                                    interval = max(1, int(e.get("interval", 30)))
                                    if text:
                                        msgs.append({"text": text, "interval": interval})
                                self._config[k] = msgs
                        elif k in _INT_KEYS:
                            try:
                                v = int(data[k])
                                if k == "thanks_cooldown_secs":
                                    v = max(1, v)
                                self._config[k] = v
                            except (TypeError, ValueError):
                                pass
                        elif k in _BOOL_KEYS:
                            self._config[k] = bool(data[k])
                        else:
                            self._config[k] = data[k]
                if "system_prompt" in data:
                    self._config["system_prompt"] = data["system_prompt"]
            self._save_env()
            self._save_settings()
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

        # ── voices ────────────────────────────────────────────────────────────

        @app.route("/api/voices")
        def api_voices():
            voices_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Voices")
            return _flask.jsonify({"voices": _scan_voices_dir(voices_dir)})

        # ── data files ───────────────────────────────────────────────────────

        @app.route("/api/datafiles")
        def api_datafiles_list():
            os.makedirs(self._data_dir, exist_ok=True)
            try:
                files = sorted(
                    [{"name": e.name, "size": e.stat().st_size}
                     for e in os.scandir(self._data_dir)
                     if e.is_file() and _DATA_NAME_RE.fullmatch(e.name)],
                    key=lambda x: x["name"],
                )
            except Exception:
                files = []
            return _flask.jsonify({"files": files})

        @app.route("/api/datafiles/<name>", methods=["GET"])
        def api_datafile_read(name: str):
            path = _safe_data_path(self._data_dir, name)
            if path is None:
                return _flask.jsonify({"error": "invalid name"}), 400
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()
            except FileNotFoundError:
                return _flask.jsonify({"error": "not found"}), 404
            except Exception:
                return _flask.jsonify({"error": "read error"}), 500
            return _flask.jsonify({"name": name, "content": content})

        @app.route("/api/datafiles/<name>", methods=["POST"])
        def api_datafile_save(name: str):
            path = _safe_data_path(self._data_dir, name)
            if path is None:
                return _flask.jsonify({"error": "invalid name"}), 400
            data    = _flask.request.get_json(force=True, silent=True) or {}
            content = data.get("content", "")
            os.makedirs(self._data_dir, exist_ok=True)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception:
                return _flask.jsonify({"error": "write error"}), 500
            return _flask.jsonify({"ok": True, "name": name})

        @app.route("/api/datafiles/<name>", methods=["DELETE"])
        def api_datafile_delete(name: str):
            path = _safe_data_path(self._data_dir, name)
            if path is None:
                return _flask.jsonify({"error": "invalid name"}), 400
            try:
                os.remove(path)
            except FileNotFoundError:
                return _flask.jsonify({"error": "not found"}), 404
            except Exception:
                return _flask.jsonify({"error": "delete error"}), 500
            return _flask.jsonify({"ok": True})

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
                if not os.path.exists(path):
                    return _flask.jsonify({"ok": True})
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
                if not os.path.exists(path):
                    return _flask.jsonify({"ok": True})
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if name in data:
                    del data[name]
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
                if not os.path.exists(path):
                    return _flask.jsonify({"ok": True})
                with open(path, encoding="utf-8") as f:
                    quotes = json.load(f)
                filtered = [q for q in quotes if q.get("id") != quote_id]
                if len(filtered) < len(quotes):
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(filtered, f, indent=2)
            return _flask.jsonify({"ok": True})

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

    def _init_counter_presets(self) -> None:
        path = os.path.join(self._data_dir, "counters.json")
        presets = {
            "deaths": {"value": 0, "display": "Deaths: {value}", "edit_roles": ["moderator", "broadcaster"]},
            "wins":   {"value": 0, "display": "Wins: {value}",   "edit_roles": ["moderator", "broadcaster"]},
            "losses": {"value": 0, "display": "Losses: {value}", "edit_roles": ["moderator", "broadcaster"]},
        }
        os.makedirs(self._data_dir, exist_ok=True)
        with self._counters_lock:
            if os.path.exists(path):
                return
            with open(path, "w", encoding="utf-8") as f:
                json.dump(presets, f, indent=2)

    def _start_services(self) -> None:
        self._tts = TTSEngine(
            get_config=self._get_tts_cfg,
            log=self._log,
            on_audio=self._broadcast_tts_audio,
        )
        self._ai  = AIResponseHandler(
            get_config=self._get_ai_cfg,
            log=self._log,
            tts=self._tts,
            on_thinking=self._broadcast_ai_thinking,
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
        es = self._eventsub
        if es:
            es.disconnect()
            self._eventsub = None
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
        self._irc = TwitchIRCClient(
            get_creds=self._get_irc_creds,
            log=self._log,
            on_message=self._dispatch,
            on_ready=self._on_irc_ready,
            on_reconnecting=self._on_irc_reconnecting,
            on_event=self._handle_event,
        )
        self._irc.connect()
        self._log("[System] Connecting to Twitch IRC…")
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

    def _disconnect(self) -> None:
        irc = self._irc
        if irc:
            irc.disconnect()
            self._irc = None
        es = self._eventsub
        if es:
            es.disconnect()
            self._eventsub = None
        with self._config_lock:
            self._config["twitch_status"] = "off"
        self._broadcast_status()
        self._log("[System] Disconnected.")

    def _on_irc_ready(self) -> None:
        with self._config_lock:
            self._config["twitch_status"] = "online"
        self._broadcast_status()

    def _on_irc_reconnecting(self) -> None:
        with self._config_lock:
            self._config["twitch_status"] = "connecting"
        self._broadcast_status()

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
    # User role resolution
    # ══════════════════════════════════════════════════════════════════════════

    def _build_user_roles(self, username: str, badges_str: str) -> set[str]:
        """Return the set of roles for a user based on IRC badges and roles.json."""
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

    def _route_role_commands(self, username: str, message: str, user_roles: set) -> bool:
        """Handle !addrole, !removerole, !roles commands. Returns True if consumed."""
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
            if not is_mod:
                return True
            if len(parts) < 3:
                if irc and channel:
                    irc.say(channel, "Usage: !addrole <user> <role>")
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
            if not is_mod:
                return True
            if len(parts) < 3:
                if irc and channel:
                    irc.say(channel, "Usage: !removerole <user> <role>")
                return True
            target = parts[1].lower()
            role   = parts[2].lower()
            path   = os.path.join(self._data_dir, "roles.json")
            with self._roles_lock:
                custom: dict = {}
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        custom = json.load(f)
                changed = False
                if role in custom:
                    new_members = [m for m in custom[role] if m != target]
                    if len(new_members) != len(custom[role]):
                        custom[role] = new_members
                        changed = True
                if changed:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(custom, f, indent=2)
            if irc and channel:
                irc.say(channel, f"Removed '{role}' from {target}.")
            self._log(f"[Roles] {username} → !removerole {target} {role}")

        elif cmd == "!roles":
            if len(parts) < 2:
                return True
            target = parts[1].lower()
            found: set = {"everyone"}
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

    def _route_counters(self, username: str, message: str, user_roles: set) -> bool:
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
            if not (user_roles & {"moderator", "broadcaster"}):
                if irc and channel:
                    irc.say(channel, "Only moderators can add counters.")
                return False
            if len(parts) < 2:
                return False
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
            if not (user_roles & {"moderator", "broadcaster"}):
                if irc and channel:
                    irc.say(channel, "Only moderators can delete counters.")
                return False
            if len(parts) < 2:
                return False
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
                        entry["value"] = max(0, int(parts[2]))
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

    def _route_quotes(self, username: str, message: str, user_roles: set) -> bool:
        parts = message.strip().split()
        if not parts:
            return False
        word = parts[0].lower()
        if word not in ("!quote", "!quotecount", "!addquote", "!delquote"):
            return False

        with self._config_lock:
            channel       = self._config.get("twitch_channel", "").lower().strip()
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
            return f'[#{q.get("id", "?")}] {q.get("text", "")} — {q.get("author", "unknown")} ({date})'

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
            author = channel
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
                reply = _fmt(random.choice(quotes))
        else:
            reply = _fmt(random.choice(quotes))
        if irc and channel:
            irc.say(channel, reply)
        return True

    # ══════════════════════════════════════════════════════════════════════════
    # Message dispatch  (called from IRC thread)
    # ══════════════════════════════════════════════════════════════════════════

    def _dispatch(self, username: str, message: str,
                  bits: int = 0, reward_id: str = "", badges: str = "") -> None:
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

        with self._config_lock:
            bot_username = self._config.get("bot_username", "").lower()
        if bot_username and username.lower() == bot_username:
            return

        with self._history_lock:
            self._chat_history.append((username, message))

        user_roles = self._build_user_roles(username, badges)
        handled = self._route_role_commands(username, message, user_roles)
        handled = handled or self._route_counters(username, message, user_roles)
        handled = handled or self._route_quotes(username, message, user_roles)
        if not handled:
            self._route_chat_commands(username, message, user_roles)
        self._route_plays(username, message)
        self._route_ai(username, message, bits, reward_id)
        if bits > 0:
            self._handle_event("bits", username, {"bits": bits})

    def _fetch_stream_info(self) -> dict:
        with self._stream_cache_lock:
            if time.time() - self._stream_cache_ts < 60:
                return dict(self._stream_cache)
            # Pre-mark timestamp to prevent concurrent threads from also fetching.
            # Other threads arriving during the HTTP call will see a "fresh" timestamp
            # and return the (stale) cache instead of double-fetching.
            self._stream_cache_ts = time.time()

        with self._config_lock:
            channel   = self._config.get("twitch_channel", "")
            client_id = self._config.get("twitch_client_id", "")
            token     = (self._config.get("twitch_token", "")
                         or self._config.get("bot_token", ""))

        if not channel or not client_id or not token:
            return {}
        try:
            resp = requests.get(
                "https://api.twitch.tv/helix/streams",
                params={"user_login": channel},
                headers={
                    "Client-ID": client_id,
                    "Authorization": f"Bearer {token.removeprefix('oauth:')}",
                },
                timeout=5,
            )
            resp.raise_for_status()
            data   = resp.json().get("data", [])
            result = data[0] if data else {}
            with self._stream_cache_lock:
                self._stream_cache    = result
                self._stream_cache_ts = time.time()
            return dict(result)
        except Exception as exc:
            self._log(f"[StreamInfo] Fetch failed: {exc}")
        return {}

    def _route_chat_commands(self, username: str, message: str,
                             user_roles: "set[str] | None" = None) -> None:
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
            allowed = entry.get("allowed_roles", [])
            if allowed and user_roles is not None and not (user_roles & set(allowed)):
                return
            try:
                cooldown = int(entry.get("cooldown", 0) or 0)
            except (ValueError, TypeError):
                cooldown = 0
            cooldown_type = entry.get("cooldown_type", "global")
            with self._cmd_cooldowns_lock:
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
                self._cmd_use_counts[word] = self._cmd_use_counts.get(word, 0) + 1
                count = self._cmd_use_counts[word]
            response = entry.get("response", "")
            args     = message.strip()[len(word):].strip()
            stream_info = self._fetch_stream_info()
            response    = _apply_placeholders(
                response, username, channel, word, args,
                count, stream_info, self._data_dir,
            )
            irc = self._irc
            if irc and response:
                irc.say(channel, response[:500])
                self._log(f"[Commands] {username} → {word}")
        elif word == "!commands" and list_enabled:
            cmd_list = "Commands: " + ", ".join(sorted(commands.keys()))
            irc = self._irc
            if irc:
                irc.say(channel, cmd_list[:500])
                self._log(f"[Commands] {username} → !commands (auto-list)")

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
            bot_user         = self._config.get("bot_username", "").lower()
            context_enabled  = self._config.get("ai_context_enabled", False)
            context_size     = int(self._config.get("ai_context_size", 5))

        if not ai_enabled:
            return

        context: list[tuple[str, str]] | None = None
        if context_enabled and context_size > 0:
            with self._history_lock:
                hist = list(self._chat_history)
            # exclude the message we're about to process (it was just appended)
            if hist and hist[-1] == (username, message):
                hist = hist[:-1]
            context = hist[-context_size:] if hist else []

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
            with self._ai_counter_lock:
                self._ai_counter += 1
                if self._ai_counter >= max(1, every_n):
                    self._ai_counter = 0
                    triggered = True

        ai = self._ai
        if triggered and ai:
            ai.handle(username, message, context=context)

    def _handle_event(self, event_type: str, username: str, extra: dict) -> None:
        with self._config_lock:
            if not self._config.get("thanks_enabled", False):
                return
            if (self._config.get("ignore_list_enabled", False)
                    and username.lower() in self._config.get("ignore_list", [])):
                return
            event_map = {
                "follow":      self._config.get("thanks_follow",  True),
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
            with self._thanks_lock:
                now = time.time()
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


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    web = WebApp()
    port = web._config.get("web_port", 5000)
    print(f"[System] Web UI starting → http://0.0.0.0:{port}")
    print(f"[System] Open http://<your-ip>:{port} in your browser")
    web._flask.run(host="0.0.0.0", port=port, threaded=True, debug=False)
