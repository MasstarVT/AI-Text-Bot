# CLAUDE.md — Twitch Interactive Bot

## Running the project

```bash
# Existing venv already has all deps:
DISPLAY=:0 .venv/bin/python twitch_bot.py
```

The app requires a display (`DISPLAY=:0` on Linux). All dependencies are installed in `.venv/`.

## Architecture

Single file: `twitch_bot.py`. Four classes plus the app window:

| Class | Responsibility |
|---|---|
| `TwitchBotApp` | Main CTk window — UI construction, service lifecycle, message routing |
| `TwitchIRCClient` | Raw TCP socket to `irc.chat.twitch.tv:6667`, PING/PONG, PRIVMSG parsing |
| `AIResponseHandler` | Queue-backed worker; POSTs to local LLM (OpenAI-compatible endpoint) |
| `TTSEngine` | Queue-backed worker; runs Piper subprocess, plays WAV via pygame |
| `GameInputController` | Fire-and-forget key presses via pydirectinput (Windows) or pynput (Linux) |

## Key design rules

- **No GUI calls from worker threads.** All background → GUI communication goes through `_log_queue` (a `queue.Queue[str]`), drained by `_poll_logs()` via `self.after(80, ...)` on the main thread.
- `game_input_enabled` and `ai_enabled` are `ctk.BooleanVar`; `.get()` is GIL-safe for reads from any thread.
- `get_config` / `get_creds` callables are passed to workers so they always read the latest GUI field values without storing stale snapshots.

## Saved prompts

System prompts are saved as `.txt` files in `prompts/` (created next to the script on first run). Save/Load buttons are in the AI Interaction tab.

## Dependencies

| Package | Purpose |
|---|---|
| `customtkinter` | Dark-themed GUI |
| `requests` | HTTP to local LLM |
| `pygame` | WAV playback after Piper synthesis |
| `pynput` | Keyboard input on Linux/macOS |
| `pydirectinput` | Keyboard input on Windows (DirectX-compatible) |

`pygame.mixer` may fail on some Linux setups (SDL audio not compiled in) — TTS audio is skipped gracefully; everything else works.

## IRC credential format

- Channel: plain name, no `#`
- Username: bot account name (lowercase)
- Token: `oauth:xxxxxxxxxxxxxxxx` (prefix added automatically if omitted)
