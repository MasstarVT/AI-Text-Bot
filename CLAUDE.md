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

## AI trigger logic (`_route_ai`)

Four independent checkboxes — AI fires if **any** enabled condition is met:

| Condition | Config |
|---|---|
| Every N messages | entry field; counter resets after each trigger |
| @bot mentions | matches if bot username appears anywhere in the message |
| Bits cheer ≥ N | parsed from `bits` IRCv3 tag; compared against minimum field |
| Channel Point redeem | parsed from `custom-reward-id` IRCv3 tag; Reward ID field is optional (blank = any) |

Bits and channel-point data come from IRCv3 tags parsed in `TwitchIRCClient._handle`. Only text-required channel point redemptions appear in IRC; reward IDs are logged to the Console so users can copy them.

## Settings persistence (`.env`)

Connection fields (channel, username, token, LLM endpoint/model, Piper paths) are saved to `.env` next to the script whenever the user clicks **Connect**. On next launch, `_load_env()` parses the file before `_build_ui()` runs so `_build_connection` can pre-fill the entries.

`.env` is in `.gitignore` — credentials are never committed.

`.env` key names: `TWITCH_CHANNEL`, `TWITCH_USERNAME`, `TWITCH_TOKEN`, `LLM_ENDPOINT`, `LLM_MODEL`, `PIPER_EXE`, `PIPER_MODEL`, `PIPER_CONFIG`.

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
