# Performance, Reliability & UX — Design Spec
Date: 2026-07-03

## Goal

Make the bot faster, more reliable, and easier to operate during a live stream. Three performance tracks, one reliability track, one UX track — all self-contained, no new dependencies, all changes land in `twitch_bot.py` and `templates/index.html`.

---

## Track 1 — Persistent Piper process

### Problem
`TTSEngine._synthesize` spawns a new `piper` subprocess per clip. Process startup costs ~200–300ms before synthesis begins. On short sentences this is 50%+ of total latency. When the queue backs up, every clip pays this cost.

### Design
`TTSEngine.__init__` starts Piper once using:
```
piper --model <model> --json-input
```
with no `--output_file` so WAV is written to stdout.

A `_reader_thread` (daemon) runs continuously, parsing stdout into complete WAV frames:
- Read 8 bytes: verify `RIFF` magic at offset 0, parse uint32 size at offset 4
- Read `size` more bytes to get the complete WAV frame
- Base64-encode and call `on_audio(wav_b64)`
- Check `_stop_event` between frames; if set, discard the frame and continue

To synthesize, `_synthesize` writes one JSON line to Piper's stdin:
```json
{"text": "hello world"}\n
```
No subprocess spawn. No temp file.

### Panic
`panic()` sets `_stop_event`. The reader thread drops the in-progress frame at the next frame boundary. The persistent process is **not** killed — only the current frame is discarded.

### Voice model changes
`_restart_piper()` terminates the old process and starts a fresh one with the new model path. Called whenever the user saves new TTS settings.

### Error handling
If Piper crashes (bad model, OOM), the reader thread detects stdout EOF, logs the error, and retries `_restart_piper()` after a 2-second delay. Synthesis requests queue up normally during the restart window.

### What doesn't change
`speak()`, `stop()`, `on_audio`, the SSE broadcast path, OBS browser source capture — none of these are affected. Audio still plays through the browser `Audio` element exactly as before.

---

## Track 2 — Streaming AI → sentence-level TTS dispatch

### Problem
`AIResponseHandler._query` waits for the full LLM response before returning. For a 3-sentence reply, TTS doesn't start until all 3 sentences are generated. Perceived latency = full generation time + synthesis time.

### Design
`_query` switches to `stream=True`. Token chunks are accumulated in a sentence buffer. On each append, a boundary check runs: if the buffer ends with `.`, `!`, or `?` followed by whitespace or end-of-stream, the sentence is flushed to TTS immediately via a new `tts_cb` callback, and the buffer resets.

The full assembled text is still returned for logging and Discord replies.

**Provider streaming formats:**

| Provider format | Chunk extraction |
|---|---|
| OpenAI-compatible (Ollama, LM Studio, OpenAI, Grok, Gemini) | `data: {...}` SSE → `choices[0].delta.content` |
| Anthropic (Claude) | `data: {...}` SSE → `type == content_block_delta` → `delta.text` |

**Sentence boundary detection:**
- Flush on `.`, `!`, `?` followed by space or end-of-stream
- Minimum flush length of 8 characters to avoid flushing abbreviations like "Dr." alone
- Flush any remaining buffer at end-of-stream regardless of punctuation

**Panic coordination:**
If `panic()` fires mid-stream, the sentence buffer is discarded and the HTTP response body is drained and dropped. No partial sentence reaches TTS.

**TTS wiring:**
`AIResponseHandler` already holds `self.tts` (a `TTSEngine` reference). The streaming refactor replaces the single `self.tts.speak(reply)` call (after full response) with `self.tts.speak(sentence)` calls per flushed sentence during streaming. The existing `_use_tts` flag check (from `cfg.get("tts_ai", True)`) gates both paths — no new wiring needed. Discord replies already pass `use_tts=False` and are unaffected.

---

## Track 3 — Twitch IRC auto-reconnect

### Problem
When the Twitch IRC socket drops (network blip, server restart), `TwitchIRCClient` exits its loop and the bot goes silent. The user must notice and manually click Reconnect.

### Design
`TwitchIRCClient._run` wraps its connection loop in a retry loop with exponential backoff:
- Delay sequence: 1s, 2s, 4s, 8s, 16s, 30s (cap)
- Backoff resets after a clean connection lasting >60s
- On each retry attempt: update status pill to `connecting`, log `[Twitch] Reconnecting in Xs...`
- On success: status pill → `online`, backoff counter resets
- On explicit `disconnect()` call: retry loop exits cleanly (no reconnect)

`TwitchIRCClient` uses `self._running` to distinguish an intentional stop (`disconnect()` sets it to `False`) from a crash (loop exits while `_running` is still `True`) — reconnect only fires when the loop exits unexpectedly.

---

## Track 4 — UX additions

### Volume slider
- Range input (`0.0`–`1.0`, default `1.0`) added to the browser UI header bar, next to the Panic button
- Value persisted to `localStorage` as `tts_volume`
- Applied to each `Audio` object in `_ttsPlayNext` before `.play()`
- Loaded from `localStorage` on page load

### Queue-depth badge
- Integer counter on the Panic button label: `⏹ Panic (3)`
- Incremented in `_ttsEnqueue`, decremented in `_ttsPlayNext` (on dequeue) and zeroed in `_ttsPanic`
- Zero count: badge hidden, button shows `⏹ Panic`

### Voice model dropdown
- New GET `/api/voices` endpoint: scans `Voices/` directory for `*.onnx` files, returns `{"voices": ["name1", "name2"]}`
- TTS settings tab replaces the manual `.onnx` path input with a `<select>` populated from this endpoint
- Manual path input remains as a fallback beneath the dropdown for paths outside `Voices/`
- On dropdown change, the path field updates automatically

### AI thinking indicator
- New SSE events: `ai-thinking` (fired when AI handler dequeues a request) and `ai-done` (fired when response is complete or errored)
- Browser: small animated "● AI thinking…" label appears in the AI panel header while processing, hidden otherwise
- Clears on `ai-done`, `ai-error`, or SSE reconnect

---

## What doesn't change
- File structure: still single `twitch_bot.py` + `templates/index.html`
- All existing API routes and their contracts
- Discord integration (uses `AIResponseHandler` — inherits streaming automatically)
- OBS browser source audio capture (audio still plays through browser `Audio` element)
- Settings persistence (`.env` + `settings.json`)

---

## Implementation order
1. Track 1 (persistent Piper) — most isolated, easiest to test
2. Track 3 (Twitch auto-reconnect) — no dependencies on 1 or 2
3. Track 4 UX items (volume, badge, voices dropdown) — no dependencies
4. Track 2 (streaming AI) — depends on Track 1 being stable (TTS must handle rapid sentence dispatches)
5. Track 4 AI thinking indicator — depends on Track 2 streaming events
