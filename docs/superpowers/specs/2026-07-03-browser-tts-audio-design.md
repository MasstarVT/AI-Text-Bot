# Browser TTS Audio — Design Spec
**Date:** 2026-07-03

## Goal

Route Piper TTS audio to the browser instead of playing it on the server. The panic button must still silence audio in the browser immediately.

## Approach

Base64-encode the synthesized WAV and broadcast it to all SSE clients as a named `tts` event. The browser decodes it and plays it via the Web Audio API. A separate `tts-panic` SSE event triggers immediate stop + queue clear in the browser.

## Backend changes (`twitch_bot.py`)

### `TTSEngine`

- `__init__` gains an `on_audio: Callable[[str], None] | None = None` parameter (receives base64-encoded WAV string).
- `_synthesize`: after Piper exits successfully and `_stop_event` is not set, read the temp WAV bytes, base64-encode them, and call `on_audio(wav_b64)`. The temp file is still cleaned up in the `finally` block as before.
- `_play()` is removed. `pygame` / system-player fallback code is removed from the TTS path. (`HAS_PYGAME` / `pygame` imports can remain for any future use.)
- `_stop_event` guard before calling `on_audio` preserves existing panic-during-synthesis behaviour.

### `WebApp`

- `_start_services` passes `on_audio=self._broadcast_tts_audio` when constructing `TTSEngine`.
- New helper `_broadcast_tts_audio(wav_b64: str)`: pushes `event: tts\ndata: {"wav": "<wav_b64>"}\n\n` to all SSE clients (same pattern as `_broadcast_status`).
- `/api/tts/panic` adds one line after calling `self._tts.panic()`: broadcast `event: tts-panic\ndata: {}\n\n` to all SSE clients.

## Frontend changes (`templates/index.html`)

### Audio queue

```js
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

### SSE listeners (added inside `startSSE`)

```js
es.addEventListener('tts',       e => _ttsEnqueue(JSON.parse(e.data).wav));
es.addEventListener('tts-panic', () => _ttsPanic());
```

## Data flow

```
Piper subprocess
    → temp .wav written
    → WAV bytes read + base64-encoded
    → on_audio(b64) called on TTS worker thread
        → _broadcast_tts_audio pushes SSE event
            → browser(s) decode b64, enqueue, play sequentially
```

## Panic flow

```
Browser: click ⏹ Panic
    → POST /api/tts/panic
        → self._tts.panic()          # drains queue, kills Piper, sets _stop_event
        → broadcast tts-panic SSE    # browser clears queue + pauses active clip
```

## Edge cases

- **Multiple browser tabs open**: all tabs receive the `tts` event and will all play audio. This is the same behaviour as if multiple people had the UI open — acceptable given this is a single-operator tool.
- **No browser connected**: `on_audio` broadcasts to zero clients; audio is silently dropped. This is intentional (the user chose browser-only playback).
- **Panic mid-synthesis**: `_stop_event` is checked before `on_audio` is called, so no WAV is sent to the browser for the killed clip.
- **Large WAV**: a 10-second clip at 22050 Hz 16-bit mono ≈ 440 KB raw → ~587 KB base64. Acceptable for SSE.
- **Autoplay policy**: browsers block `audio.play()` until the user has interacted with the page. The `.catch(_ttsPlayNext)` handler skips the blocked clip and moves to the next, rather than hanging the queue.

## Files changed

| File | Change |
|---|---|
| `twitch_bot.py` | `TTSEngine`: add `on_audio` param, call it instead of `_play`, remove `_play`. `WebApp`: add `_broadcast_tts_audio`, pass it to `TTSEngine`, broadcast `tts-panic` from panic endpoint. |
| `templates/index.html` | Add TTS audio queue functions, `tts` + `tts-panic` SSE listeners. |
