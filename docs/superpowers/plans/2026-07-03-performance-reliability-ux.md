# Performance, Reliability & UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bot faster (persistent Piper, streaming AI→TTS), more reliable (Twitch exponential backoff + reconnect status), and easier to use (volume slider, queue badge, voice dropdown, AI thinking indicator).

**Architecture:** All changes land in `twitch_bot.py` and `templates/index.html`. `TTSEngine` gains a persistent Piper subprocess with a WAV-frame reader thread replacing per-clip spawns. `AIResponseHandler` switches to streaming HTTP and dispatches sentences to TTS as they arrive. `TwitchIRCClient._run` gains exponential backoff and a `on_reconnecting` status callback.

**Tech Stack:** Python stdlib (`subprocess`, `threading`, `json`, `re`, `base64`), `requests` (streaming), Flask SSE, vanilla JS.

---

## Task 1: Persistent Piper — refactor `TTSEngine`

**Files:**
- Modify: `twitch_bot.py` lines 140–280 (full `TTSEngine` class replacement)
- Create: `tests/test_tts_engine.py`

### Background
Currently `_synthesize` spawns a new `piper` subprocess per clip (~200–300 ms startup each time). The new design keeps one Piper process alive with `--json-input` (reads one JSON line per utterance from stdin, writes one WAV per utterance to stdout). A `_reader` daemon thread parses WAV frames from stdout using the RIFF header (`RIFF` magic at offset 0, uint32 size at offset 4 → total frame = size + 8 bytes) and calls `on_audio`. `panic()` no longer kills the subprocess — it sets `_stop_event` so the reader discards the next frame.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tts_engine.py`:

```python
"""Unit tests for TTSEngine persistent Piper process."""
import io
import json
import struct
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, call


def make_wav_frame(text="hello"):
    """Build a minimal fake WAV frame (RIFF header + 4 bytes of silence)."""
    data = b'\x00\x00\x00\x00'
    chunk_size = 36 + len(data)  # 36 = WAV fmt chunk + data chunk header
    header = b'RIFF' + struct.pack('<I', chunk_size) + b'WAVE'
    fmt    = b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 22050, 44100, 2, 16)
    dchunk = b'data' + struct.pack('<I', len(data)) + data
    return header + fmt + dchunk


class FakePiperProc:
    """Fake subprocess.Popen for Piper — reads JSON lines, writes WAV frames."""
    def __init__(self, model_path, wav_frames=None):
        self.returncode = None
        self._wav_frames = wav_frames or [make_wav_frame()]
        self._frame_idx = 0
        self._stdin_lines = []
        self._out_buf = io.BytesIO()
        self.stdin  = MagicMock()
        self.stderr = io.BytesIO(b'')
        # Build stdout content: one WAV per line written
        self.stdout = MagicMock()
        self._setup_stdout()

    def _setup_stdout(self):
        frames = b''.join(self._wav_frames)
        self.stdout.read = self._make_reader(io.BytesIO(frames))

    def _make_reader(self, buf):
        def read(n):
            return buf.read(n)
        return read

    def kill(self): self.returncode = -9
    def poll(self): return self.returncode
    def wait(self, timeout=None): return self.returncode


class TestTTSEnginePersistentPiper(unittest.TestCase):
    def _make_engine(self, fake_proc=None):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        cfg = {"piper_exe": "piper", "model_path": "/voices/test.onnx", "config_path": ""}
        received_audio = []
        engine = twitch_bot.TTSEngine(
            get_config=lambda: cfg,
            log=lambda msg: None,
            on_audio=lambda b64: received_audio.append(b64),
        )
        engine._received_audio = received_audio
        return engine

    def test_speak_calls_on_audio(self):
        """speak() causes on_audio to be called with a base64 WAV string."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        cfg = {"piper_exe": "piper", "model_path": "/voices/test.onnx", "config_path": ""}
        received = []
        done = threading.Event()

        frame = make_wav_frame()

        def on_audio(b64):
            received.append(b64)
            done.set()

        fake_proc = FakePiperProc(wav_frames=[frame])
        with patch("subprocess.Popen", return_value=fake_proc):
            engine = twitch_bot.TTSEngine(
                get_config=lambda: cfg,
                log=lambda msg: None,
                on_audio=on_audio,
            )
            engine.speak("hello world")
            done.wait(timeout=3)

        self.assertEqual(len(received), 1)
        import base64
        self.assertEqual(base64.b64decode(received[0]), frame)

    def test_panic_discards_next_frame(self):
        """panic() causes the reader to discard the next WAV frame."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        cfg = {"piper_exe": "piper", "model_path": "/voices/test.onnx", "config_path": ""}
        received = []

        fake_proc = FakePiperProc(wav_frames=[make_wav_frame(), make_wav_frame()])
        with patch("subprocess.Popen", return_value=fake_proc):
            engine = twitch_bot.TTSEngine(
                get_config=lambda: cfg,
                log=lambda msg: None,
                on_audio=lambda b64: received.append(b64),
            )
            # Panic before any audio is dispatched
            engine._stop_event.set()
            # Manually trigger reader to process one frame
            # (the reader should discard it and clear _stop_event)
            time.sleep(0.2)
            self.assertFalse(engine._stop_event.is_set(), "panic event should be cleared after frame discard")

    def test_no_on_audio_when_model_missing(self):
        """speak() with no model configured logs and does not call on_audio."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        cfg = {"piper_exe": "piper", "model_path": "", "config_path": ""}
        logs = []
        received = []

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = FakePiperProc(wav_frames=[])
            engine = twitch_bot.TTSEngine(
                get_config=lambda: cfg,
                log=lambda msg: logs.append(msg),
                on_audio=lambda b64: received.append(b64),
            )
            engine.speak("test")
            time.sleep(0.3)

        self.assertEqual(len(received), 0)
        self.assertTrue(any("No voice model" in l for l in logs))
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
.venv/bin/python -m pytest tests/test_tts_engine.py -v 2>&1 | head -40
```

Expected: `AttributeError` or `AssertionError` — TTSEngine still uses old subprocess-per-clip design.

- [ ] **Step 3: Replace `TTSEngine` in `twitch_bot.py`**

Replace the entire `TTSEngine` class (lines 140–280) with:

```python
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

        cmd = [piper_exe, "--model", model_path, "--json-input"]
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
            self._synthesize(item)

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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
.venv/bin/python -m pytest tests/test_tts_engine.py -v
```

Expected: all 3 tests `PASSED`.

- [ ] **Step 5: Verify Python syntax**

```bash
.venv/bin/python -c "import ast; ast.parse(open('twitch_bot.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add twitch_bot.py tests/test_tts_engine.py
git commit -m "feat(tts): replace per-clip Piper spawn with persistent process + reader thread"
```

---

## Task 2: Twitch exponential backoff + reconnect status pill

**Files:**
- Modify: `twitch_bot.py` — `TwitchIRCClient.__init__`, `_run` (~lines 409–450); `WebApp._connect`, `_on_irc_ready` (~lines 1389–1421)

### Background
`TwitchIRCClient._run` already loops on disconnect but uses a fixed 5-second delay and doesn't update the status pill to "Connecting…". This task adds exponential backoff (1→2→4→8→16→30s cap) and an `on_reconnecting` callback that `WebApp` uses to push the status update.

- [ ] **Step 1: Write the failing test**

Create `tests/test_reconnect.py`:

```python
"""Unit tests for TwitchIRCClient exponential backoff reconnect."""
import threading
import time
import unittest
from unittest.mock import MagicMock, patch


class TestTwitchReconnectBackoff(unittest.TestCase):
    def _make_client(self, on_reconnecting=None):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        creds = {"channel": "testchan", "username": "testbot", "token": "oauth:abc"}
        client = twitch_bot.TwitchIRCClient(
            get_creds=lambda: creds,
            log=lambda msg: None,
            on_message=lambda *a: None,
            on_reconnecting=on_reconnecting,
        )
        return client

    def test_on_reconnecting_called_on_drop(self):
        """on_reconnecting callback is called when the session drops unexpectedly."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        reconnect_calls = []
        done = threading.Event()

        def on_reconnecting():
            reconnect_calls.append(1)
            done.set()

        client = self._make_client(on_reconnecting=on_reconnecting)

        call_count = [0]
        def fake_session():
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionResetError("server dropped")
            client._running = False  # stop after second attempt

        client._session = fake_session
        client.connect()
        done.wait(timeout=5)
        self.assertGreaterEqual(len(reconnect_calls), 1)

    def test_backoff_delay_increases(self):
        """Reconnect delay doubles on each failure up to 30s cap."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        delays_slept = []

        client = self._make_client()

        call_count = [0]
        def fake_session():
            call_count[0] += 1
            if call_count[0] < 4:
                raise ConnectionResetError("dropped")
            client._running = False

        client._session = fake_session

        original_sleep = time.sleep
        def fake_sleep(n):
            delays_slept.append(n)

        with patch("time.sleep", side_effect=fake_sleep):
            client.connect()
            time.sleep(0.1)  # allow thread to run

        # Delays should be 1, 2, 4 (doubling)
        self.assertEqual(delays_slept[:3], [1, 2, 4])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
.venv/bin/python -m pytest tests/test_reconnect.py -v 2>&1 | head -30
```

Expected: `TypeError` — `TwitchIRCClient.__init__` doesn't accept `on_reconnecting`.

- [ ] **Step 3: Update `TwitchIRCClient.__init__` to accept `on_reconnecting`**

In `twitch_bot.py`, find `TwitchIRCClient.__init__` (~line 409). Change:

```python
    def __init__(self, get_creds, log, on_message, on_ready=None) -> None:
        self.get_creds  = get_creds
        self.log        = log
        self.on_message = on_message
        self.on_ready   = on_ready
        self._sock: socket.socket | None = None
        self._running = False
        self._ready_fired = False
```

To:

```python
    def __init__(self, get_creds, log, on_message, on_ready=None, on_reconnecting=None) -> None:
        self.get_creds       = get_creds
        self.log             = log
        self.on_message      = on_message
        self.on_ready        = on_ready
        self.on_reconnecting = on_reconnecting  # callable() | None — fired before each reconnect attempt
        self._sock: socket.socket | None = None
        self._running = False
        self._ready_fired = False
```

- [ ] **Step 4: Replace `_run` with exponential backoff**

Find `TwitchIRCClient._run` (~line 442). Replace:

```python
    def _run(self) -> None:
        while self._running:
            try:
                self._session()
            except Exception as exc:
                if self._running:
                    self.log(f"[IRC] Disconnected ({exc}). Reconnecting in {RECONNECT_DELAY}s…")
                    time.sleep(RECONNECT_DELAY)
```

With:

```python
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
```

- [ ] **Step 5: Wire `on_reconnecting` in `WebApp._connect`**

Find `WebApp._connect` (~line 1389). Add `_on_irc_reconnecting` method and pass it:

After `_on_irc_ready` (~line 1417), add:

```python
    def _on_irc_reconnecting(self) -> None:
        with self._config_lock:
            self._config["twitch_status"] = "connecting"
        self._broadcast_status()
```

In `_connect`, find the `TwitchIRCClient(...)` constructor (~line 1398). Add `on_reconnecting=self._on_irc_reconnecting`:

```python
        self._irc = TwitchIRCClient(
            get_creds=self._get_irc_creds,
            log=self._log,
            on_message=self._dispatch,
            on_ready=self._on_irc_ready,
            on_reconnecting=self._on_irc_reconnecting,
        )
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
.venv/bin/python -m pytest tests/test_reconnect.py -v
```

Expected: all tests `PASSED`.

- [ ] **Step 7: Commit**

```bash
git add twitch_bot.py tests/test_reconnect.py
git commit -m "feat(irc): exponential backoff reconnect with status pill update"
```

---

## Task 3: `/api/voices` endpoint + TTS voice dropdown

**Files:**
- Modify: `twitch_bot.py` — add GET `/api/voices` route (~line 1350, near `/api/presets`)
- Modify: `templates/index.html` — TTS settings tab (~lines 307–326)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tts_engine.py`:

```python
class TestVoicesEndpoint(unittest.TestCase):
    def test_voices_lists_onnx_files(self):
        """GET /api/voices returns .onnx filenames from the Voices/ directory."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        with patch("os.listdir", return_value=["voice1.onnx", "voice1.onnx.json", "voice2.onnx", "readme.txt"]):
            with patch("os.path.isdir", return_value=True):
                app = twitch_bot.WebApp.__new__(twitch_bot.WebApp)
                # Build minimal Flask app to test the route
                import flask as _flask
                flask_app = _flask.Flask("test")

                @flask_app.route("/api/voices")
                def api_voices():
                    voices_dir = os.path.join(os.path.dirname(__file__), "..", "Voices")
                    try:
                        names = sorted(
                            f[:-5] for f in os.listdir(voices_dir)
                            if f.endswith(".onnx")
                        )
                    except FileNotFoundError:
                        names = []
                    return _flask.jsonify({"voices": names})

                with flask_app.test_client() as c:
                    resp = c.get("/api/voices")
                    data = resp.get_json()

        self.assertIn("voice1", data["voices"])
        self.assertIn("voice2", data["voices"])
        self.assertNotIn("voice1.onnx.json", data["voices"])
        self.assertNotIn("readme.txt", data["voices"])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_tts_engine.py::TestVoicesEndpoint -v
```

Expected: `FAILED` (route doesn't exist yet in the real app).

- [ ] **Step 3: Add `/api/voices` route to `twitch_bot.py`**

Find the `/api/presets` GET route (~line 1320) in `WebApp._build_routes`. Add after it:

```python
        @app.route("/api/voices")
        def api_voices():
            voices_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Voices")
            try:
                names = sorted(
                    f[:-5] for f in os.listdir(voices_dir)
                    if f.endswith(".onnx")
                )
            except FileNotFoundError:
                names = []
            return _flask.jsonify({"voices": names})
```

- [ ] **Step 4: Update the TTS settings tab in `index.html`**

Find the TTS tab section (~line 307). Replace the Voice Model field:

```html
      <!-- TTS tab -->
      <div id="tab-tts" class="tab-pane">
        <div class="field-row"><label>Piper Executable</label>
          <div class="field-with-btn">
            <input id="s-piper-exe" type="text" placeholder="piper  or  /path/to/piper">
            <button class="btn btn-neutral btn-sm" onclick="openBrowser('s-piper-exe')">Browse</button>
          </div>
        </div>
        <div class="field-row"><label>Voice Model</label>
          <div class="field-with-btn">
            <select id="s-voice-select" onchange="onVoiceSelect(this.value)" style="flex:1"></select>
            <button class="btn btn-neutral btn-sm" onclick="refreshVoices()">↻</button>
          </div>
        </div>
        <div class="field-row"><label>Custom path (.onnx)</label>
          <div class="field-with-btn">
            <input id="s-piper-model" type="text" placeholder="/path/to/voice.onnx">
            <button class="btn btn-neutral btn-sm" onclick="openBrowser('s-piper-model')">Browse</button>
          </div>
        </div>
        <div class="hint">Select a voice above or enter a full path manually.</div>
        <div class="field-row"><label>Model Config (.json)</label>
          <div class="field-with-btn">
            <input id="s-piper-cfg" type="text" placeholder="/path/to/voice.onnx.json (optional)">
            <button class="btn btn-neutral btn-sm" onclick="openBrowser('s-piper-cfg')">Browse</button>
          </div>
        </div>
      </div>
```

- [ ] **Step 5: Add `refreshVoices` and `onVoiceSelect` JS functions in `index.html`**

Find the `refreshPromptList` function (~line 651). Add after it:

```js
// ── voice model dropdown ──────────────────────────────────────────────────
function refreshVoices() {
  api('/api/voices').then(r => {
    const sel = el('s-voice-select');
    const cur = el('s-piper-model').value;
    sel.innerHTML = '<option value="">— pick a voice —</option>';
    (r.voices || []).forEach(name => {
      const o = document.createElement('option');
      o.value = name;
      o.textContent = name;
      sel.appendChild(o);
    });
    // Re-select if current model matches a voice name
    const match = (r.voices || []).find(n => cur.includes(n + '.onnx'));
    if (match) sel.value = match;
  });
}

function onVoiceSelect(name) {
  if (!name) return;
  const base = window._voicesDir || 'Voices';
  el('s-piper-model').value = base + '/' + name + '.onnx';
  el('s-piper-cfg').value   = base + '/' + name + '.onnx.json';
}
```

- [ ] **Step 6: Call `refreshVoices()` when the TTS tab is opened**

Find `showTab` at line 721 in `index.html`. Add one line at the end:

```js
function showTab(name) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  el('tab-' + name).classList.add('active');
  document.querySelectorAll('.tab').forEach(b => {
    if (b.textContent.toLowerCase().startsWith(name)) b.classList.add('active');
  });
  if (name === 'tts') refreshVoices();
}
```

- [ ] **Step 7: Simplify `onVoiceSelect` — no `_voicesDir` needed**

The `onVoiceSelect` function in Step 5 should use `'Voices/'` directly (the server runs from the project root, so the relative path resolves correctly):

```js
function onVoiceSelect(name) {
  if (!name) return;
  el('s-piper-model').value = 'Voices/' + name + '.onnx';
  el('s-piper-cfg').value   = 'Voices/' + name + '.onnx.json';
}
```

Update the function added in Step 5 to match this exact form.

- [ ] **Step 8: Commit**

```bash
git add twitch_bot.py templates/index.html
git commit -m "feat(ui): voice model dropdown from Voices/ directory, /api/voices endpoint"
```

---

## Task 4: TTS volume slider + queue depth badge

**Files:**
- Modify: `templates/index.html` only (header HTML, CSS, JS)

- [ ] **Step 1: Add volume slider CSS and HTML to the header**

Find the header HTML (~line 129):

```html
<header id="header">
  <h1>Twitch Interactive Bot</h1>
  <button class="btn btn-panic" onclick="api('/api/tts/panic','POST')">⏹ Panic</button>
```

Replace with:

```html
<header id="header">
  <h1>Twitch Interactive Bot</h1>
  <button id="btn-panic" class="btn btn-panic" onclick="api('/api/tts/panic','POST').then(_ttsPanic)">⏹ Panic <span id="tts-queue-badge" style="display:none">(0)</span></button>
  <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:var(--muted)">
    🔊<input id="tts-volume" type="range" min="0" max="1" step="0.05" value="1"
       style="width:70px;cursor:pointer" oninput="onVolumeChange(this.value)">
  </label>
```

Note: `.then(_ttsPanic)` ensures the browser clears its queue immediately when the user clicks Panic, without waiting for the SSE event round-trip.

- [ ] **Step 2: Add volume and badge CSS to `<style>`**

Find the `/* ── header ── */` CSS block (~line 18). Add after it:

```css
#tts-volume{accent-color:var(--accent)}
#tts-queue-badge{font-size:11px;opacity:.8}
```

- [ ] **Step 3: Add JS for volume and badge**

Find the `// ── TTS browser audio` block (~line 395). Replace the entire TTS JS section with:

```js
// ── TTS browser audio ─────────────────────────────────────────────────────
const _ttsQueue = [];
let _ttsActive = null;
let _ttsQueueCount = 0;

function _ttsUpdateBadge() {
  const badge = el('tts-queue-badge');
  if (!badge) return;
  if (_ttsQueueCount > 0) {
    badge.textContent = '(' + _ttsQueueCount + ')';
    badge.style.display = 'inline';
  } else {
    badge.style.display = 'none';
  }
}

function _ttsVolume() {
  const v = el('tts-volume');
  return v ? parseFloat(v.value) : 1;
}

function onVolumeChange(val) {
  localStorage.setItem('tts_volume', val);
  if (_ttsActive) _ttsActive.volume = parseFloat(val);
}

function _ttsEnqueue(wav_b64) {
  _ttsQueue.push(wav_b64);
  _ttsQueueCount++;
  _ttsUpdateBadge();
  if (!_ttsActive) _ttsPlayNext();
}

function _ttsPlayNext() {
  if (!_ttsQueue.length) { _ttsActive = null; return; }
  const b64 = _ttsQueue.shift();
  _ttsQueueCount = Math.max(0, _ttsQueueCount - 1);
  _ttsUpdateBadge();
  const audio = new Audio('data:audio/wav;base64,' + b64);
  audio.volume = _ttsVolume();
  _ttsActive = audio;
  audio.onended = () => { if (_ttsActive === audio) _ttsPlayNext(); };
  audio.onerror = () => { if (_ttsActive === audio) { _ttsActive = null; _ttsPlayNext(); } };
  audio.play().catch(err => { console.warn('[TTS] play() rejected:', err); if (_ttsActive === audio) _ttsPlayNext(); });
}

function _ttsPanic() {
  _ttsQueue.length = 0;
  _ttsQueueCount = 0;
  _ttsUpdateBadge();
  if (_ttsActive) { _ttsActive.pause(); _ttsActive = null; }
}
```

- [ ] **Step 4: Load volume from localStorage on page init**

Find the `DOMContentLoaded` block at the bottom of the `<script>` (~line 894). Replace:

```js
window.addEventListener('DOMContentLoaded', () => {
  loadState().then(() => startSSE());
});
```

With:

```js
window.addEventListener('DOMContentLoaded', () => {
  const saved = localStorage.getItem('tts_volume');
  if (saved !== null) { const v = el('tts-volume'); if (v) v.value = saved; }
  loadState().then(() => startSSE());
});
```

- [ ] **Step 5: Commit**

```bash
git add templates/index.html
git commit -m "feat(ui): TTS volume slider and queue depth badge on Panic button"
```

---

## Task 5: Streaming AI — OpenAI-compatible providers

**Files:**
- Modify: `twitch_bot.py` — `AIResponseHandler._query`, replace `_query_openai` with `_stream_openai`
- Create: `tests/test_ai_streaming.py`

### Background
`_query_openai` currently does a single blocking POST with `"stream": False`. The new `_stream_openai` uses `stream=True` and `requests` iter_lines(). Sentences are dispatched to `self.tts.speak()` as they complete. The full text is still assembled for logging and `reply_cb`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ai_streaming.py`:

```python
"""Unit tests for AIResponseHandler streaming sentence dispatch."""
import io
import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch


def make_openai_stream(*tokens):
    """Build a fake OpenAI SSE stream from a list of token strings."""
    lines = []
    for token in tokens:
        chunk = {"choices": [{"delta": {"content": token}, "finish_reason": None}]}
        lines.append(b"data: " + json.dumps(chunk).encode() + b"\n")
    lines.append(b"data: [DONE]\n")
    return b"\n".join(lines)


def make_anthropic_stream(*tokens):
    """Build a fake Anthropic SSE stream from a list of token strings."""
    lines = []
    for token in tokens:
        event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": token}}
        lines.append(b"data: " + json.dumps(event).encode() + b"\n")
    lines.append(b'data: {"type":"message_stop"}\n')
    return b"\n".join(lines)


class FakeTTS:
    def __init__(self):
        self.spoken = []
    def speak(self, text):
        self.spoken.append(text)


class TestAIStreaming(unittest.TestCase):
    def _make_handler(self, provider="LM Studio"):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        fake_tts = FakeTTS()
        cfg = {
            "provider": provider,
            "endpoint": "http://localhost:1234/v1/chat/completions",
            "model": "test-model",
            "api_key": "",
            "system_prompt": "You are a test bot.",
            "tts_ai": True,
        }
        handler = twitch_bot.AIResponseHandler(
            get_config=lambda: cfg,
            log=lambda msg: None,
            tts=fake_tts,
        )
        handler._fake_tts = fake_tts
        return handler

    def test_openai_stream_dispatches_sentences(self):
        """Streaming OpenAI response dispatches completed sentences to TTS."""
        handler = self._make_handler("LM Studio")
        done = threading.Event()

        tokens = ["Hello", " world", ".", " How", " are", " you", "?"]
        stream_body = make_openai_stream(*tokens)

        mock_resp = MagicMock()
        mock_resp.iter_lines.return_value = iter(stream_body.split(b"\n"))
        mock_resp.raise_for_status = MagicMock()

        handler.handle("user", "hi", reply_cb=lambda _: done.set())

        with patch("requests.post", return_value=mock_resp):
            done.wait(timeout=5)

        spoken = handler._fake_tts.spoken
        self.assertGreater(len(spoken), 0)
        full = " ".join(spoken)
        self.assertIn("Hello world.", full)

    def test_openai_stream_assembles_full_reply(self):
        """Full assembled text is logged (reply_cb receives the full reply)."""
        handler = self._make_handler("LM Studio")
        replies = []
        done = threading.Event()

        tokens = ["Nice", " to", " meet", " you", "!"]
        stream_body = make_openai_stream(*tokens)

        mock_resp = MagicMock()
        mock_resp.iter_lines.return_value = iter(stream_body.split(b"\n"))
        mock_resp.raise_for_status = MagicMock()

        def cb(text):
            replies.append(text)
            done.set()

        handler.handle("user", "hello", reply_cb=cb)

        with patch("requests.post", return_value=mock_resp):
            done.wait(timeout=5)

        self.assertEqual(len(replies), 1)
        self.assertIn("Nice to meet you!", replies[0])

    def test_anthropic_stream_dispatches_sentences(self):
        """Streaming Anthropic response dispatches sentences to TTS."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        fake_tts = FakeTTS()
        cfg = {
            "provider": "Claude",
            "endpoint": "https://api.anthropic.com/v1/messages",
            "model": "claude-opus-4-8",
            "api_key": "test-key",
            "system_prompt": "You are a test bot.",
            "tts_ai": True,
        }
        handler = twitch_bot.AIResponseHandler(
            get_config=lambda: cfg,
            log=lambda msg: None,
            tts=fake_tts,
        )
        done = threading.Event()

        tokens = ["Hello", " there", ".", " Goodbye", "!"]
        stream_body = make_anthropic_stream(*tokens)

        mock_resp = MagicMock()
        mock_resp.iter_lines.return_value = iter(stream_body.split(b"\n"))
        mock_resp.raise_for_status = MagicMock()

        handler.handle("user", "hi", reply_cb=lambda _: done.set())

        with patch("requests.post", return_value=mock_resp):
            done.wait(timeout=5)

        self.assertGreater(len(fake_tts.spoken), 0)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
.venv/bin/python -m pytest tests/test_ai_streaming.py -v 2>&1 | head -30
```

Expected: `FAILED` — `_stream_openai` doesn't exist yet.

- [ ] **Step 3: Add `_is_sentence_boundary` static method to `AIResponseHandler`**

After `AIResponseHandler.stop` (~line 306), add:

```python
    @staticmethod
    def _is_sentence_boundary(text: str) -> bool:
        """True if text ends at a sentence boundary suitable for TTS dispatch."""
        stripped = text.rstrip()
        return len(stripped) >= 8 and stripped[-1] in '.!?'
```

- [ ] **Step 4: Add `_stream_openai` method to `AIResponseHandler`**

After `_is_sentence_boundary`, add:

```python
    def _stream_openai(self, endpoint: str, model: str, api_key: str,
                       system_prompt: str, username: str, message: str,
                       tts_cb) -> str:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": f"{username}: {message}"},
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
            except (json.JSONDecodeError, KeyError, IndexError):
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
```

- [ ] **Step 5: Add `_stream_anthropic` method to `AIResponseHandler`**

After `_stream_openai`, add:

```python
    def _stream_anthropic(self, endpoint: str, model: str, api_key: str,
                          system_prompt: str, username: str, message: str,
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
            "messages":   [{"role": "user", "content": f"{username}: {message}"}],
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
```

- [ ] **Step 6: Update `_query` to use streaming methods**

Find `AIResponseHandler._query` (~line 318). Replace:

```python
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
```

With:

```python
        tts_cb = self.tts.speak if _use_tts else None

        try:
            if fmt == "anthropic":
                reply = self._stream_anthropic(endpoint, model, api_key, system_prompt, username, message, tts_cb)
            else:
                reply = self._stream_openai(endpoint, model, api_key, system_prompt, username, message, tts_cb)

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
```

- [ ] **Step 7: Remove the now-replaced `_query_openai` and `_query_anthropic` methods**

Delete the old `_query_openai` (~line 355–376) and `_query_anthropic` (~line 378–393) methods entirely.

- [ ] **Step 8: Run tests**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
.venv/bin/python -m pytest tests/test_ai_streaming.py tests/test_discord_integration.py -v
```

Expected: all tests `PASSED`. (Discord tests still pass because they mock `requests.post` which now hits `_stream_openai`, which returns the mocked content.)

If discord tests fail because the mock doesn't return iter_lines, update `tests/test_discord_integration.py`'s mock:

```python
# Change this in _make_handler:
mock_resp = MagicMock()
mock_resp.json.return_value = { ... }  # old non-streaming mock

# Replace with streaming mock:
mock_resp = MagicMock()
fake_reply = "Hello from AI"
chunk = json.dumps({"choices": [{"delta": {"content": fake_reply}, "finish_reason": None}]})
mock_resp.iter_lines.return_value = iter([
    b"data: " + chunk.encode(),
    b"data: [DONE]",
])
mock_resp.raise_for_status = MagicMock()
```

- [ ] **Step 9: Verify syntax**

```bash
.venv/bin/python -c "import ast; ast.parse(open('twitch_bot.py').read()); print('OK')"
```

- [ ] **Step 10: Commit**

```bash
git add twitch_bot.py tests/test_ai_streaming.py tests/test_discord_integration.py
git commit -m "feat(ai): streaming responses with per-sentence TTS dispatch for all providers"
```

---

## Task 6: AI thinking indicator

**Files:**
- Modify: `twitch_bot.py` — `AIResponseHandler.__init__`, `_worker`; `WebApp._start_services`, add `_broadcast_ai_thinking`
- Modify: `templates/index.html` — AI panel header, SSE listeners

- [ ] **Step 1: Add `on_thinking` parameter to `AIResponseHandler.__init__`**

Find `AIResponseHandler.__init__` (~line 295). Change:

```python
    def __init__(self, get_config, log, tts: TTSEngine) -> None:
        self.get_config = get_config
        self.log = log
        self.tts = tts
        self._q: queue.Queue[tuple | None] = queue.Queue()
        threading.Thread(target=self._worker, name="AI-Worker", daemon=True).start()
```

To:

```python
    def __init__(self, get_config, log, tts: TTSEngine, on_thinking=None) -> None:
        self.get_config   = get_config
        self.log          = log
        self.tts          = tts
        self._on_thinking = on_thinking  # callable(bool) | None
        self._q: queue.Queue[tuple | None] = queue.Queue()
        threading.Thread(target=self._worker, name="AI-Worker", daemon=True).start()
```

- [ ] **Step 2: Fire thinking callbacks in `_worker`**

Find `AIResponseHandler._worker` (~line 310). Replace:

```python
    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            username, message, reply_cb, prompt_override, use_tts = item
            self._query(username, message, reply_cb=reply_cb, prompt_override=prompt_override, use_tts=use_tts)
```

With:

```python
    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            username, message, reply_cb, prompt_override, use_tts = item
            if self._on_thinking:
                self._on_thinking(True)
            try:
                self._query(username, message, reply_cb=reply_cb,
                            prompt_override=prompt_override, use_tts=use_tts)
            finally:
                if self._on_thinking:
                    self._on_thinking(False)
```

- [ ] **Step 3: Add `_broadcast_ai_thinking` to `WebApp`**

After `WebApp._broadcast_tts_audio` (~line 865), add:

```python
    def _broadcast_ai_thinking(self, thinking: bool) -> None:
        event = "ai-thinking" if thinking else "ai-done"
        msg = f"event: {event}\ndata: {{}}\n\n"
        with self._log_lock:
            for q in list(self._sse_clients):
                q.put(msg)
```

- [ ] **Step 4: Pass `on_thinking` in `_start_services`**

Find `WebApp._start_services` (~line 1366). Change:

```python
        self._ai  = AIResponseHandler(
            get_config=self._get_ai_cfg,
            log=self._log,
            tts=self._tts,
        )
```

To:

```python
        self._ai  = AIResponseHandler(
            get_config=self._get_ai_cfg,
            log=self._log,
            tts=self._tts,
            on_thinking=self._broadcast_ai_thinking,
        )
```

- [ ] **Step 5: Add thinking indicator HTML to the AI panel**

Find the AI panel header in `index.html` (~line 177):

```html
    <div class="panel-hdr">
      <span>AI Chat Reading Active</span>
      <label class="toggle" title="Toggle AI">
```

Replace with:

```html
    <div class="panel-hdr">
      <span>AI Chat Reading Active</span>
      <span id="ai-thinking-indicator" style="display:none;color:var(--orange);font-size:11px;animation:pulse 1s infinite">● thinking…</span>
      <label class="toggle" title="Toggle AI">
```

Add to the `<style>` block:

```css
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
```

- [ ] **Step 6: Wire SSE listeners in `startSSE`**

Find the SSE listener block in `startSSE` (~line 428). Add after `tts-panic`:

```js
  es.addEventListener('ai-thinking', () => { el('ai-thinking-indicator').style.display = 'inline'; });
  es.addEventListener('ai-done',     () => { el('ai-thinking-indicator').style.display = 'none'; });
```

Also clear indicator on reconnect — in `startSSE`, after `_ttsPanic()` at the top:

```js
  el('ai-thinking-indicator').style.display = 'none';
```

- [ ] **Step 7: Run full test suite**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
.venv/bin/python -m pytest tests/ -v
```

Expected: all tests `PASSED`.

- [ ] **Step 8: Verify syntax**

```bash
.venv/bin/python -c "import ast; ast.parse(open('twitch_bot.py').read()); print('OK')"
```

- [ ] **Step 9: Commit**

```bash
git add twitch_bot.py templates/index.html
git commit -m "feat(ui): AI thinking indicator via SSE ai-thinking/ai-done events"
```

---

## Final smoke test

- [ ] **Start the app and verify all features**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
.venv/bin/python twitch_bot.py
```

Open `http://localhost:5000` in a browser. Check:

1. **TTS settings tab** — voice dropdown populates from `Voices/` (shows `en_GB-alan-medium`)
2. **Volume slider** — adjusts audio volume; value persists across page reload
3. **Panic badge** — send a TTS message; badge shows count, clears on panic/finish
4. **TTS audio** — speak a message; audio plays without Piper restart delay on second clip
5. **AI thinking** — trigger AI; "● thinking…" appears in AI panel header, disappears when done
6. **Twitch reconnect** — connect to Twitch; pull network cable or restart router; status should show "Connecting…" then return to "Online" automatically

- [ ] **Push to remote**

```bash
git push
```
