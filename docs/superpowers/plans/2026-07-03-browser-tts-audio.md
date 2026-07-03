# Browser TTS Audio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route Piper TTS audio to the browser via SSE instead of playing it on the server machine, with panic support.

**Architecture:** After Piper synthesizes a WAV, `TTSEngine` base64-encodes it and calls an `on_audio` callback instead of playing locally. `WebApp` supplies a callback that broadcasts an SSE `tts` event to all connected browsers. The browser maintains a sequential audio queue and plays clips via `new Audio(dataURL)`. Panic broadcasts a `tts-panic` SSE event that clears the browser queue and pauses active playback.

**Tech Stack:** Python (standard library `base64`), Flask SSE, vanilla JS Web Audio API (`new Audio()`).

---

## Files

| File | Change |
|---|---|
| `twitch_bot.py` | `TTSEngine.__init__`: add `on_audio` param. `_synthesize`: call `on_audio` instead of `_play`. Remove `_play`. `WebApp`: add `_broadcast_tts_audio` method, pass it to `TTSEngine`, broadcast `tts-panic` from panic endpoint. |
| `templates/index.html` | Add TTS audio queue functions + `tts` / `tts-panic` SSE listeners. |

---

## Task 1: Add `on_audio` callback to `TTSEngine` and remove local playback

**Files:**
- Modify: `twitch_bot.py:158-165` (`__init__`), `twitch_bot.py:274` (`_synthesize` call site), `twitch_bot.py:290-335` (remove `_play`)

- [ ] **Step 1: Update `TTSEngine.__init__` to accept `on_audio`**

Replace lines 158–165 in `twitch_bot.py`:

```python
    def __init__(self, get_config, log, on_audio=None) -> None:
        self.get_config = get_config
        self.log = log
        self.on_audio = on_audio          # callable(wav_b64: str) | None
        self._q: queue.Queue[str | None] = queue.Queue()
        self._stop_event = threading.Event()
        self._current_proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        threading.Thread(target=self._worker, name="TTS-Worker", daemon=True).start()
```

- [ ] **Step 2: Add `import base64` to the top-level imports**

In `twitch_bot.py`, find the stdlib imports block (lines 30–41). Add `import base64` after `import asyncio` so the block is alphabetically ordered:

```python
import asyncio
import base64
import collections
import json
```

- [ ] **Step 3: Replace `self._play(tmp_path)` in `_synthesize` with `on_audio` call**

In `twitch_bot.py`, find line 274 (`self._play(tmp_path)`) and replace it with:

```python
            if self.on_audio and not self._stop_event.is_set():
                with open(tmp_path, "rb") as _f:
                    wav_b64 = base64.b64encode(_f.read()).decode("ascii")
                self.on_audio(wav_b64)
```

- [ ] **Step 4: Remove the `_play` method entirely**

Delete lines 287–335 in `twitch_bot.py` — from the `# System audio players tried in order...` comment through the end of `_play`, including `_SYSTEM_PLAYERS`. The method is no longer called anywhere.

- [ ] **Step 5: Verify no `_play` references remain**

Run:
```bash
grep -n "_play\|pygame.mixer\|HAS_PYGAME.*audio\|_SYSTEM_PLAYERS" twitch_bot.py
```

Expected: zero results (the `HAS_PYGAME` variable and `pygame` import can stay; only audio-playback usage is gone).

- [ ] **Step 6: Commit**

```bash
git add twitch_bot.py
git commit -m "refactor(tts): replace local playback with on_audio callback"
```

---

## Task 2: Add `_broadcast_tts_audio` to `WebApp` and wire it to `TTSEngine`

**Files:**
- Modify: `twitch_bot.py:897-907` (after `_broadcast_status`), `twitch_bot.py:1413-1414` (`_start_services`)

- [ ] **Step 1: Add `_broadcast_tts_audio` method to `WebApp`**

After the `_broadcast_status` method (around line 907), add:

```python
    def _broadcast_tts_audio(self, wav_b64: str) -> None:
        """Push a TTS audio clip to all SSE clients."""
        msg = f"event: tts\ndata: {json.dumps({'wav': wav_b64})}\n\n"
        with self._log_lock:
            for q in list(self._sse_clients):
                q.put(msg)
```

- [ ] **Step 2: Pass `on_audio` to `TTSEngine` in `_start_services`**

Replace line 1414 in `twitch_bot.py`:

```python
        self._tts = TTSEngine(
            get_config=self._get_tts_cfg,
            log=self._log,
            on_audio=self._broadcast_tts_audio,
        )
```

- [ ] **Step 3: Verify the app starts without errors**

Run:
```bash
.venv/bin/python twitch_bot.py &
sleep 2
curl -s http://localhost:5000/api/state | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if 'tts_ai' in d else 'bad')"
kill %1
```

Expected output: `ok`

- [ ] **Step 4: Commit**

```bash
git add twitch_bot.py
git commit -m "feat(tts): broadcast WAV via SSE on_audio callback"
```

---

## Task 3: Broadcast `tts-panic` SSE event from the panic endpoint

**Files:**
- Modify: `twitch_bot.py:1170-1176` (`api_tts_panic`)

- [ ] **Step 1: Update `api_tts_panic` to broadcast `tts-panic` SSE event**

Replace the existing `api_tts_panic` function (lines 1170–1176):

```python
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
```

- [ ] **Step 2: Commit**

```bash
git add twitch_bot.py
git commit -m "feat(tts): broadcast tts-panic SSE event on panic"
```

---

## Task 4: Add browser audio queue and SSE listeners to the frontend

**Files:**
- Modify: `templates/index.html` (JS section, `startSSE` function)

- [ ] **Step 1: Add the TTS audio queue functions**

In `templates/index.html`, find the `// ── SSE stream ────` comment (around line 395). Directly above it, insert:

```js
// ── TTS browser audio ─────────────────────────────────────────────────────
const _ttsQueue = [];
let _ttsActive = null;

function _ttsEnqueue(wav_b64) {
  _ttsQueue.push(wav_b64);
  if (!_ttsActive) _ttsPlayNext();
}

function _ttsPlayNext() {
  if (!_ttsQueue.length) { _ttsActive = null; return; }
  const b64 = _ttsQueue.shift();
  _ttsActive = new Audio('data:audio/wav;base64,' + b64);
  _ttsActive.onended = _ttsPlayNext;
  _ttsActive.play().catch(_ttsPlayNext);
}

function _ttsPanic() {
  _ttsQueue.length = 0;
  if (_ttsActive) { _ttsActive.pause(); _ttsActive = null; }
}

```

- [ ] **Step 2: Wire `tts` and `tts-panic` SSE listeners inside `startSSE`**

In `templates/index.html`, find `startSSE` (around line 396). After the existing `es.addEventListener('status', ...)` block and before `es.onerror`, add:

```js
  es.addEventListener('tts',       e => _ttsEnqueue(JSON.parse(e.data).wav));
  es.addEventListener('tts-panic', () => _ttsPanic());
```

The complete `startSSE` function should look like:

```js
function startSSE() {
  const es = new EventSource('/stream');
  es.onmessage = e => log(e.data);
  es.addEventListener('status', e => {
    const s = JSON.parse(e.data);
    applyStatus(s.twitch_status, s.discord_status);
  });
  es.addEventListener('tts',       e => _ttsEnqueue(JSON.parse(e.data).wav));
  es.addEventListener('tts-panic', () => _ttsPanic());
  es.onerror = () => { es.close(); setTimeout(startSSE, 3000); };
}
```

- [ ] **Step 3: Commit**

```bash
git add templates/index.html
git commit -m "feat(frontend): add browser TTS audio queue via SSE"
```

---

## Task 5: Manual end-to-end verification

- [ ] **Step 1: Start the app**

```bash
.venv/bin/python twitch_bot.py
```

Open `http://localhost:5000` in a browser.

- [ ] **Step 2: Verify TTS plays in browser**

In Settings → TTS tab, configure a valid Piper executable and voice model. Enable "Speak AI replies via TTS". Send a manual AI message via the web UI. Confirm audio plays **in the browser** (not on the server).

Check the browser console — there should be no errors related to `tts` event parsing or `Audio` playback (autoplay policy warnings are acceptable if no prior interaction).

- [ ] **Step 3: Verify panic stops browser audio**

While audio is playing, click the **⏹ Panic** button. Confirm the audio stops immediately in the browser.

- [ ] **Step 4: Verify no local audio**

On the server, confirm no pygame or system audio player is invoked (no sound from the server machine, no `pygame` errors in the console log).

- [ ] **Step 5: Push**

```bash
git push
```
