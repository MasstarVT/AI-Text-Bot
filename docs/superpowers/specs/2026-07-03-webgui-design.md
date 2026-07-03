# Web GUI Design — Twitch Interactive Bot

**Date:** 2026-07-03  
**Status:** Approved

## Overview

Replace the CustomTkinter desktop GUI with a Flask-based web UI so the bot can run headless and be accessed across a trusted local network. The five backend service classes (IRC, AI, TTS, Discord, GameInput) are unchanged. Only the presentation layer is replaced.

---

## Architecture

### Threading model

```
Main thread      Flask app.run(host='0.0.0.0', port=WEB_PORT, threaded=True)
IRC thread       TwitchIRCClient._run()           ← unchanged
AI thread        AIResponseHandler._worker()      ← unchanged
TTS thread       TTSEngine._worker()              ← unchanged
Discord thread   DiscordClient._run()             ← unchanged
Input threads    short-lived per keypress         ← unchanged
SSE threads      one per connected browser tab (Flask threaded=True)
```

### State management

| Current (CTk)              | New (WebApp)                              |
|----------------------------|-------------------------------------------|
| `ctk.BooleanVar`           | plain `bool` protected by `_state_lock`   |
| `CTkEntry.get()`           | `_config` dict updated on settings save   |
| `_prompt_cache/_lock`      | kept as-is, populated via REST POST       |
| `_discord_prompt_cache`    | kept as-is, populated via REST POST       |

`get_config()` and `get_creds()` remain callables returning snapshots of `_config` — the worker threads' interface is unchanged.

### Log broadcast

`_log()` fans out to a list of per-SSE-connection `queue.Queue` objects protected by `_sse_clients_lock`. Each SSE client's queue is added on connect and removed on disconnect. This replaces the single `_log_queue` → GUI polling pattern.

A fixed-size ring buffer (`collections.deque(maxlen=200)`) stores the last 200 log lines. `GET /api/state` includes this buffer so a freshly opened browser tab sees startup messages rather than a blank console.

### File structure

```
twitch_bot.py          # WebApp class replaces TwitchBotApp; Flask routes added
templates/
  index.html           # Single-page web UI (HTML + embedded CSS + JS)
.env                   # Includes WEB_PORT (default 5000)
```

CustomTkinter and related GUI imports (`customtkinter`, `tkinter.filedialog`) are removed. `pygame` stays for TTS audio playback.

---

## API Endpoints

### Real-time
| Method | Path      | Description                                      |
|--------|-----------|--------------------------------------------------|
| GET    | `/stream` | SSE log stream; one persistent connection per tab |

### Page
| Method | Path         | Description                                        |
|--------|--------------|----------------------------------------------------|
| GET    | `/`          | Serves `index.html`                                |
| GET    | `/api/state` | Full current state: status, toggles, config, commands |

### Lifecycle
| Method | Path                    | Description            |
|--------|-------------------------|------------------------|
| POST   | `/api/connect`          | Connect Twitch IRC     |
| POST   | `/api/disconnect`       | Disconnect Twitch IRC  |
| POST   | `/api/discord/connect`  | Connect Discord bot    |
| POST   | `/api/discord/disconnect` | Disconnect Discord   |

### Settings
| Method | Path            | Description                              |
|--------|-----------------|------------------------------------------|
| GET    | `/api/settings` | Return all config fields                 |
| POST   | `/api/settings` | Save fields to `_config` and write `.env` |

### Controls
| Method | Path               | Description                        |
|--------|--------------------|------------------------------------|
| POST   | `/api/ai/toggle`   | Toggle AI enabled on/off           |
| POST   | `/api/input/toggle`| Toggle game input enabled on/off   |
| POST   | `/api/tts/panic`   | Stop TTS immediately               |
| POST   | `/api/commands`    | Replace full command map (JSON)    |

### Models & Prompts
| Method | Path                    | Description                           |
|--------|-------------------------|---------------------------------------|
| GET    | `/api/models`           | Fetch model list from provider (`?provider=`) |
| GET    | `/api/prompts`          | List `.txt` files in `prompts/`       |
| GET    | `/api/prompts/<name>`   | Load prompt text                      |
| POST   | `/api/prompts/<name>`   | Save prompt text                      |

### File Browser
| Method | Path          | Description                                             |
|--------|---------------|---------------------------------------------------------|
| GET    | `/api/browse` | `?path=` returns `{dirs:[...], files:[...]}` JSON; defaults to script dir |

### Configuration
- `WEB_PORT` in `.env` sets the Flask port (default `5000`)
- Flask binds to `0.0.0.0` so it is reachable across the local network

---

## Frontend UI (`templates/index.html`)

Single HTML file with embedded CSS and vanilla JS. No build step, no external frameworks.

### Layout

**Header bar**
- Title
- Twitch: Connect / Disconnect buttons · status pill (Off / Connecting / Online / Error)
- Discord: Connect / Disconnect buttons · status pill

**Main two-column area**

Left — **Twitch Plays**
- Enable toggle
- Command table: key / duration columns, delete button per row, Add Command button
- Preset save / load

Right — **AI Interaction**
- Enable toggle
- AI trigger config: every N messages, @mention, bits ≥ N, channel point reward ID
- System prompt textarea
- Save / Load prompt buttons (dropdown of saved prompts)

**Console**
- Auto-scrolling `<pre>` block fed by the SSE stream
- Clear button

**Settings panel** (modal, opened via gear icon in header)
- Tab: Twitch — channel, username, token (masked, show/hide toggle)
- Tab: Discord — token (masked), channel ID, trigger mode dropdown, "Use shared prompt" checkbox, Discord prompt textarea
- Tab: AI — provider dropdown (auto-fills endpoint on change), endpoint field, model dropdown + Refresh button, API key (masked)
- Tab: TTS — Piper executable (text + Browse button), voice model (text + Browse button), config path (text + Browse button)
- Save button: `POST /api/settings`, updates live config

**File browser modal**
- Opens when a Browse button is clicked
- Shows current path as breadcrumb, clickable directory navigation, file list
- Clicking a file closes the modal and populates the originating field
- Cancel button

### JS behaviour

- On load: `GET /api/state` populates all fields and status indicators
- `new EventSource('/stream')` appends lines to the console
- Toggle buttons call their POST endpoint and flip visual state
- Provider dropdown `change` event auto-fills endpoint and calls model refresh
- Settings Save collects all field values into JSON and calls `POST /api/settings`

---

## .env additions

```
WEB_PORT=5000
```

All existing `.env` keys are preserved. `WEB_PORT` is loaded in `_load_env()` before Flask starts.

---

## Dependencies

**Added:** `flask`  
**Removed:** `customtkinter` (and its `tkinter` dependency; `DISPLAY` env var no longer required)  
**Unchanged:** `requests`, `pygame`, `pynput`, `pydirectinput`, `discord.py`

`requirements.txt` is updated accordingly.

---

## Security

Trusted local network — no authentication. Flask runs with `debug=False` in production mode. The `/api/browse` endpoint is restricted to absolute paths that exist on the server filesystem (no path traversal).
