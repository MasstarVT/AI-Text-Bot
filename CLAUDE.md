# CLAUDE.md — Twitch Interactive Bot

## Running the project

```bash
# Start the web server (headless — no display required):
.venv/bin/python twitch_bot.py
```

Flask binds to `0.0.0.0` on port 5000 by default.
Access the UI at `http://<server-ip>:5000` from any device on the network.

Set `WEB_PORT=<port>` in `.env` to change the port.

## Architecture

Single file: `twitch_bot.py`. Seven classes:

| Class | Responsibility |
|---|---|
| `WebApp`             | Flask web server, service lifecycle, API routes, SSE log broadcast |
| `TwitchIRCClient` | Raw TCP socket to `irc.chat.twitch.tv:6667`, PING/PONG, PRIVMSG parsing |
| `EventSubClient` | Twitch EventSub WebSocket client; subscribes to `channel.follow` and fires `on_event` callbacks |
| `AIResponseHandler` | Queue-backed worker; POSTs to local LLM (OpenAI-compatible endpoint) |
| `TTSEngine` | Queue-backed worker; runs Piper subprocess, streams WAV via SSE |
| `GameInputController` | Fire-and-forget key presses via pydirectinput (Windows) or pynput (Linux) |
| `DiscordClient` | Discord bot on a daemon thread with its own asyncio loop; filters messages by trigger mode and routes them through the shared `AIResponseHandler` |

## Key design rules

- **All mutable state lives in `WebApp._config`**, protected by `_config_lock` (a `threading.Lock`). Worker threads never write directly to `_config` — only `WebApp` methods do so under the lock.
- `get_config` / `get_creds` callables are passed to workers (`TTSEngine`, `AIResponseHandler`, `TwitchIRCClient`, `EventSubClient`, `DiscordClient`) so they always snapshot the latest config without storing stale references.
- `game_input_enabled` and `ai_enabled` use `_BoolGetter` (a thin wrapper around a `bool` field in `_config`). `.get()` reads are protected by `_config_lock`.
- When reading a shared object reference from a worker thread (e.g. `self._ai`, `self._sock`), **snapshot it to a local variable first** (`ai = self._ai`) before the truthiness check and use. This closes the TOCTOU window where the main thread can null the reference between the check and the call.
- Background threads log via `self._log(msg)` which fans out to all connected SSE clients. No direct writes to the console widget from worker threads.

## AI trigger logic (`_route_ai`)

Four independent checkboxes — AI fires if **any** enabled condition is met:

| Condition | Config |
|---|---|
| Every N messages | entry field; counter resets after each trigger |
| @bot mentions | matches if bot username appears anywhere in the message |
| Bits cheer ≥ N | parsed from `bits` IRCv3 tag; compared against minimum field |
| Channel Point redeem | parsed from `custom-reward-id` IRCv3 tag; Reward ID field is optional (blank = any) |

Bits and channel-point data come from IRCv3 tags parsed in `TwitchIRCClient._handle`. Only text-required channel point redemptions appear in IRC; reward IDs are logged to the Console so users can copy them.

## Thank-you responses (`_handle_event`)

When enabled, the bot responds to Twitch channel events with AI-generated thank-you messages.

Supported events:

| Event | Source | Config key |
|---|---|---|
| New follow | EventSub WebSocket (`channel.follow`) | `thanks_follow` |
| New sub | IRC USERNOTICE `sub` | `thanks_sub` |
| Resub | IRC USERNOTICE `resub` | `thanks_resub` |
| Gifted sub | IRC USERNOTICE `subgift` | `thanks_gift` |
| Mystery gift subs | IRC USERNOTICE `submysterygift` | `thanks_mystery` |
| Bits cheer | IRC PRIVMSG `bits` tag | `thanks_bits` |
| Raid | IRC USERNOTICE `raid` | `thanks_raid` |

IRC events arrive via `TwitchIRCClient.on_event`; follow events arrive via `EventSubClient.on_event`. Both are wired to `WebApp._handle_event` at connect time. Event messages use `_THANKS_TEMPLATES` (module-level constant). The thank-you prompt (`thanks_prompt`) is separate from the main system prompt — falls back to `_DEFAULT_THANKS_PROMPT` if blank.

**EventSub requirements:** Follow events require a Broadcaster Token with `moderator:read:followers` scope and a valid Twitch Client ID. If any of those are missing, `EventSubClient` skips the subscription and logs a warning.

Delivery is independently toggled: `thanks_chat` (post to IRC via `irc.say(channel, reply)`) and `thanks_tts` (passed as `use_tts` to `ai.handle`).

**Note:** If both `trigger_bits` and `thanks_bits` are enabled simultaneously, a bits cheer fires two separate AI calls (one from `_route_ai`, one from `_handle_event`) with different system prompts.

**Thanks cooldown (`thanks_cooldown_enabled` / `thanks_cooldown_secs`):** An optional cooldown prevents the thanks system from firing more than once within a configurable window (default 30 s). `_last_thanks_time` and `_thanks_lock` protect the timestamp; the cooldown is checked inside `_handle_event` after the per-event toggle check and before the AI call.

**Shared prompt (`thanks_use_shared_prompt`):** When enabled, `_handle_event` passes `prompt_override=None` to `ai.handle()`, causing it to fall back to the main system prompt instead of the dedicated thank-you prompt.

## Username ignore list (`_dispatch`)

When `ignore_list_enabled` is `True`, `_dispatch` returns early for any username in `ignore_list` — no AI, no plays, no commands, no thanks, no chat history. The check runs after the log line and before all routing. `_handle_event` performs the same check independently (under `_config_lock`) so USERNOTICE events (subs, raids, gifts) from ignored users are also suppressed.

Config keys: `ignore_list_enabled` (bool), `ignore_list` (list of lowercase strings).

**Bot self-filter:** Regardless of the ignore list setting, `_dispatch` always drops messages where the sender username matches `bot_username` (case-insensitive). This prevents the bot's own chat messages from triggering AI responses and is not configurable.

## Custom `!command` responses (`_route_chat_commands`)

When `chat_commands_enabled` is `True` and a message starts with a registered `!word`, `_route_chat_commands` posts the configured reply to Twitch chat without invoking the AI. Called from `_dispatch` after the ignore check and before `_route_plays`. Commands are stored as `dict[str, dict]` in `chat_commands` (keys normalised to lowercase, auto-prefixed with `!`).

Each command entry has three fields:

| Field | Type | Description |
|---|---|---|
| `response` | `str` | Reply text; placeholder substitution applied; truncated to 500 chars |
| `cooldown` | `int` | Seconds between allowed uses; `0` = no cooldown |
| `cooldown_type` | `str` | `"global"` (channel-wide timer) or `"user"` (per-viewer timer) |

**Migration:** Settings saved with the old `dict[str, str]` format are promoted on load: `"Hey!"` → `{"response": "Hey!", "cooldown": 0, "cooldown_type": "global"}`.

Response strings support placeholder substitution via `_apply_placeholders` (module-level, `twitch_bot.py`):

| Placeholder | Value |
|---|---|
| `%user%` | Twitch login username of the chatter |
| `%channel%` | Twitch channel name |
| `%command%` | The command word (always lowercase, e.g. `!so`) |
| `%args%` | Everything after the command word; empty string if nothing |

Unknown placeholders (e.g. `%usr%`) are left as-is. Responses are truncated to 500 chars **after** substitution. See `Placeholder.md` in the repo root for user-facing docs.

**Cooldown tracking:** Two in-memory dicts on `WebApp` — `_cmd_global_cooldowns: dict[str, float]` and `_cmd_user_cooldowns: dict[tuple[str, str], float]` — store last-fired timestamps. These are never persisted; they reset on bot restart.

**Auto `!commands` list (`cmd_list_enabled`):** When enabled, `_route_chat_commands` responds to `!commands` with an alphabetically sorted, comma-separated list of all registered commands prefixed with `"Commands: "`, truncated to 500 chars. A user-defined `!commands` entry always takes priority.

**Note:** If an AI trigger (e.g. every-N counter) fires on the same message as a command match, both responses go to chat. This is by design — the two systems are independent.

## Scheduled messages (`_scheduler_loop`)

A daemon thread (`Scheduler`) fires messages to Twitch chat on repeating intervals while connected. Checks every 30 seconds. Only fires when `twitch_status == "online"` and a channel is configured.

Each entry is `{"text": str, "interval": int}` (interval in minutes). Stored in `scheduled_msgs` config key.

Implementation details:
- `last_fired` is keyed by `(text, interval)` tuple — two entries with identical text but different intervals are tracked independently.
- The loop body is wrapped in `try/except Exception` so any error logs `[Scheduler] Error: ...` and the thread continues rather than dying silently.
- Stale keys are pruned each iteration using an `active_keys` set built during the loop.

## Chat context window (`_chat_history`)

When `ai_context_enabled` is `True`, the last N chat messages are prepended to the AI's user-turn as a `[Recent chat]` block. `N` is controlled by `ai_context_size` (default 5, max stored 20).

- `_chat_history: collections.deque[tuple[str, str]]` (maxlen=20) and `_history_lock` live on `WebApp`.
- `_dispatch` appends `(username, message)` after the ignore check (ignored users never enter history).
- `_route_ai` snapshots the deque under `_history_lock`, strips the current trigger message from the tail (to avoid sending it twice), slices to `context_size`, and passes `context` to `ai.handle()`.
- `AIResponseHandler._query` builds `user_content`:
  - With context: `f"[Recent chat]\n{ctx_lines}\n\n{username}: {message}"`
  - Without: `f"{username}: {message}"`
- `_stream_openai` and `_stream_anthropic` now take `user_content: str` instead of `username, message` separately.
- Thanks and Discord reply paths call `ai.handle()` without `context` (defaults to `None`) — Twitch chat history is never injected into those responses.

Lock ordering: `_config_lock` is always released before `_history_lock` is acquired — no deadlock risk.

## TTS panic / stop behaviour

`TTSEngine` has two shutdown paths:

| Method | What it does |
|---|---|
| `stop()` | Enqueues `None` sentinel — worker exits cleanly after finishing the current item |
| `panic()` | Drains the queue (re-enqueuing any `None` sentinel it finds), sets `_stop_event`, stops pygame, kills the active Piper subprocess |

`_worker` logic after dequeue:
1. If `_stop_event` is set → **clear it** (consume the panic) then `continue` (skip this item). The *next* item will synthesise normally.
2. If not set → `_synthesize(item)`.

`_proc_lock` protects `_current_proc`. Both `panic()` and `_synthesize()` check `_stop_event` inside the lock before starting a subprocess, so there is no window where a process can be launched after a panic.

## Bundled Piper TTS (`piper/`)

Piper TTS 2023.11.14-2 (Linux x86_64) is extracted into `piper/` next to the script. The directory is git-ignored (25 MB binary bundle). On startup, `_build_connection` checks for `piper/piper` and auto-fills the Piper Executable field if the `.env` entry is blank — so users don't need to configure anything beyond their voice model.

To re-download: `curl -L https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz | tar -xz`

## Discord integration (`DiscordClient`)

`DiscordClient` runs `discord.py` on a dedicated daemon thread with its own asyncio event loop. It filters incoming messages by one of four trigger modes, then hands them to the shared `AIResponseHandler` with a `reply_cb` that posts the AI reply back to the originating channel. TTS is suppressed for Discord replies (`use_tts=False`).

**Trigger modes** (`DiscordClient.TRIGGER_MODES`):

| Mode | When AI fires |
|---|---|
| All messages | Every message in the configured channel |
| @mention only | Messages that @mention the bot |
| @mention + replies | @mentions and replies to the bot |
| All messages + mentions + replies | All of the above |

**Auto-connect on startup:** if `DISCORD_TOKEN` and `DISCORD_CHANNEL_ID` are both set in `.env`, `TwitchBotApp.__init__` schedules `_discord_connect` via `self.after(1200, ...)` so the bot connects automatically.

**Discord prompt:** `_get_discord_cfg()` reads from `_config` under `_config_lock`. When "Use shared AI prompt" is checked, it returns an empty string (resolved to `None` inside `DiscordClient`, which then falls back to the main system prompt).

**Status label:** `_lbl_discord_status` in the header bar shows `Discord: ● Off / Connecting… / Online / Error` (column 5, next to the Twitch status label). `_on_ready_cb` / `_on_failure_cb` callbacks update it from the GUI thread via `self.after(0, ...)`.

**UI controls** (Connection Settings → Discord Bot tab):
- Bot Token, Channel ID entry fields
- Trigger mode combobox
- "Enable Message Content Intent" warning label
- "Use shared AI prompt" checkbox — hides/shows the Discord-specific prompt textbox
- Connect / Disconnect buttons

## Settings persistence (`.env`)

Connection fields are saved to `.env` next to the script whenever the user clicks **Connect** (Twitch) or **Connect Discord** (Discord). On next launch, `_load_env()` parses the file before `_build_ui()` runs so `_build_connection` can pre-fill the entries.

`.env` is in `.gitignore` — credentials are never committed.

`.env` key names:
- Twitch (broadcaster/EventSub): `TWITCH_CHANNEL`, `TWITCH_USERNAME`, `TWITCH_CLIENT_ID`, `TWITCH_TOKEN`
- Bot account: `BOT_USERNAME`, `BOT_TOKEN`
- LLM: `LLM_PROVIDER`, `LLM_ENDPOINT`, `LLM_MODEL`, `LLM_API_KEY`
- Piper: `PIPER_EXE`, `PIPER_MODEL`, `PIPER_CONFIG`
- Discord: `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_TRIGGER`, `DISCORD_USE_SHARED_PROMPT`, `DISCORD_PROMPT`
- Web server: `WEB_PORT`

## AI providers

Six providers are supported, selected via the **Provider** dropdown in Connection Settings:

| Provider | Format | Auth |
|---|---|---|
| Ollama | OpenAI-compatible | none |
| LM Studio | OpenAI-compatible | none |
| OpenAI | OpenAI | Bearer API key |
| Grok (xAI) | OpenAI-compatible | Bearer API key |
| Gemini | OpenAI-compatible (v1beta) | Bearer API key |
| Claude | Anthropic Messages API | `x-api-key` header |

Changing provider auto-fills the endpoint and calls Refresh on the model dropdown. Claude uses a hardcoded model list (`_CLAUDE_MODELS`); all others hit the provider's `/v1/models` (or `/api/tags` for Ollama native) endpoint. The request format branches in `AIResponseHandler._query` on `_PROVIDERS[provider]["fmt"]`.

## UI layout

The UI is served by Flask at `http://<server-ip>:5000` (`templates/index.html`).

Main page layout:
- **Header** — Panic (stop TTS), TTS volume slider, Connect / Disconnect (Twitch + Discord), Twitch and Discord status badges.
- **Left panel** — Twitch Plays: command→key mappings, preset selector, toggle on/off.
- **Right panel** — AI Interaction: trigger checkboxes (every N, @mentions, bits, channel points), system prompt textarea, prompt save/load.
- **Console** — live log output streamed via SSE.
- **Manual input bar** — send a one-off message to the AI.
- **Settings modal** (⚙ gear button) — tabbed settings popup with tabs:
  - **Twitch** — channel, broadcaster credentials, bot credentials
  - **AI** — LLM provider/endpoint/model/key
  - **Discord** — token, channel ID, trigger mode, prompt
  - **TTS** — Piper executable, model, config paths
  - **Thanks** — per-event toggles, cooldown, shared/dedicated prompt
  - **Ignore** — ignore list toggle and username list
  - **Commands** — custom `!commands` table, cooldowns, auto-list toggle
  - **Schedule** — scheduled message entries (text + interval)

## Saved prompts

System prompts are saved as `.txt` files in `prompts/` (created next to the script on first run). Save/Load buttons are in the right panel (AI Interaction) of the web UI.

## Voices directory

`Voices/` stores Piper voice models next to the script. `.onnx` binaries are git-ignored (large); `.json` config sidecars are committed.

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

- Channel: plain name, no `#` — the channel the bot joins
- Bot Username: the bot account's Twitch login name (lowercase)
- Bot OAuth Token: `oauth:xxxxxxxxxxxxxxxx` for the bot account (prefix added automatically if omitted)
- Broadcaster Username / Broadcaster Token / Client ID: optional — only needed for EventSub follow-event detection

**Migration:** If `BOT_USERNAME`/`BOT_TOKEN` are absent from `.env`, the bot automatically falls back to `TWITCH_USERNAME`/`TWITCH_TOKEN` so existing single-account setups continue working without reconfiguration.
