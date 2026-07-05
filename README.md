# Twitch Interactive Bot

A customizable Twitch stream interaction tool with Twitch Plays game control, AI chat responses via a local LLM, Piper TTS voice output, and Discord bot integration. Inspired by DougDoug's stream setups.

## Features

- **Twitch Plays** — map chat commands (e.g. `!jump`) to keyboard keys with configurable hold durations; save and load presets
- **AI Chat Responses** — connects to a local LLM (Ollama / LM Studio / OpenAI / Grok / Gemini / Claude) and responds to chat on multiple configurable triggers
- **Flexible AI Triggers** — fire the AI on every N messages, @mentions, bits cheers (minimum threshold), or specific channel point redemptions
- **Custom `!commands`** — register instant text responses to any `!word`; supports per-command cooldowns (global or per-viewer), `%user%` / `%channel%` / `%args%` placeholders, and an optional auto-generated `!commands` list
- **Thank-you Responses** — AI-generated replies to new follows, subs, resubs, gifted subs, raids, and bits cheers; configurable per event, with cooldown and shared/dedicated prompt options
- **Scheduled Messages** — post recurring messages to chat on a per-entry interval (in minutes) while connected
- **Text-to-Speech** — speaks AI replies aloud using [Piper TTS](https://github.com/rhasspy/piper); Piper binary is bundled, so no separate install is needed
- **Discord Bot** — connect a Discord bot to a channel; the AI responds to messages using the same LLM with configurable trigger modes (@mention only, all messages, etc.)
- **System Prompt Manager** — save and load named system prompts from the `prompts/` folder; Discord can use a shared or separate prompt
- **Username Ignore List** — prevent specific users from triggering any bot response
- **Bot Account Support** — use a separate bot account for IRC (chat posting) while keeping the broadcaster account for EventSub follow detection
- **Web UI** — browser-based control panel served at `http://<server-ip>:5000`; works on any device on the local network without a display

## Requirements

- Python 3.10+
- A modern browser on any device on your local network
- A running local LLM server (Ollama or LM Studio) or an API key for OpenAI / Grok / Gemini / Claude
- A Piper `.onnx` voice model (optional, for audio — the binary is bundled in `piper/`)

## Installation

```bash
# Clone / download the repo, then:
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

```bash
python twitch_bot.py
```

Then open `http://localhost:5000` (or `http://<server-ip>:5000` from another device on the network) in a browser.

1. **Header bar** — click **Connect** after filling in credentials, or **Disconnect** to stop. Status indicators show live Twitch and Discord connection states. The **⏹ Panic** button immediately stops TTS playback and clears the queue.
2. **Left panel (Twitch Plays)** — add `!command → key` mappings, set hold duration, and toggle game input on/off. Use presets to save and restore mapping sets.
3. **Right panel (AI Interaction)** — enable AI responses, configure trigger conditions (every N messages, @mentions, bits, channel points), edit the system prompt, and save/load prompts from the `prompts/` folder.
4. **Console** — live log output streamed from all threads.
5. **Manual input bar** — send a one-off message directly to the AI.
6. **⚙ Settings** — click the gear button to open the settings modal, which has tabs for:
   - **Twitch** — channel, broadcaster credentials (optional, for follow events), and bot account credentials
   - **AI** — LLM provider, endpoint, model, and API key
   - **Discord** — token, channel ID, trigger mode, and system prompt
   - **TTS** — Piper executable, voice model, and config paths
   - **Thanks** — enable/disable AI thank-you responses per event type, set a cooldown, and choose shared or dedicated prompt
   - **Ignore** — toggle the ignore list and manage blocked usernames
   - **Commands** — add custom `!command` → response entries with per-command cooldowns (global or per-viewer) and enable the auto `!commands` list
   - **Schedule** — add recurring chat messages with per-entry intervals (in minutes)

### Discord bot setup

1. Create a bot at [discord.com/developers/applications](https://discord.com/developers/applications) and copy its token.
2. Enable the **Message Content Intent** under Bot → Privileged Gateway Intents.
3. Invite the bot to your server with the `bot` scope and at minimum `Read Messages` + `Send Messages` permissions.
4. In Connection Settings → **Discord Bot**: paste the token, enter the numeric channel ID, choose a trigger mode, and click **Connect Discord**.

Settings are saved to `.env` and Discord auto-connects on next launch if the token and channel ID are present.

> **Note:** TTS is suppressed for Discord replies — only Twitch messages are spoken aloud.

### Getting a Twitch OAuth token

Use the built-in **Get OAuth Token** button in Connection Settings (requires a Twitch Developer Console app with `http://localhost` as a redirect URI). Copy the URL shown in the Console, open it in your browser, authorize, then copy the `access_token` value from the redirect URL and paste it into the OAuth Token field.

### Local LLM setup

- **Ollama**: `ollama serve` + `ollama pull llama3` → endpoint `http://localhost:11434/v1/chat/completions`
- **LM Studio**: start the local server → endpoint `http://localhost:1234/v1/chat/completions`
- **OpenAI / Grok / Gemini / Claude**: select the provider from the dropdown; the endpoint is filled automatically. Enter your API key in the API Key field.

## Project structure

```
AI Text Bot/
├── twitch_bot.py      # Main application
├── requirements.txt
├── templates/
│   └── index.html     # Web UI (served by Flask)
├── .env               # Saved connection settings (git-ignored, created on first Connect)
├── settings.json      # Bot state (AI toggles, commands, triggers — auto-saved)
├── piper/             # Bundled Piper TTS binary (git-ignored)
│   └── piper          # The executable — auto-detected and pre-filled in the UI
├── Voices/            # Voice model files — .onnx binaries are git-ignored; .json configs committed
├── prompts/           # Saved system prompts (created automatically)
├── plays_presets/     # Saved Twitch Plays key-mapping presets (created automatically)
└── README.md
```

> **Note:** `.env` is listed in `.gitignore` so your Twitch token and credentials are never committed.

## Threading model

| Thread | Role |
|---|---|
| Main | Flask web server + SSE broadcast |
| IRC | Raw TCP socket reader, auto-reconnects |
| EventSub | Twitch EventSub WebSocket (follow events) |
| AI | HTTP requests to local LLM |
| TTS | Persistent Piper process; streams WAV via SSE |
| Scheduler | Fires recurring chat messages on interval |
| Discord | `discord.py` asyncio event loop (daemon thread) |
| Input | Short-lived threads per key press |

Cross-thread communication is done through `queue.Queue` objects and SSE; worker threads never write directly to shared state without holding `_config_lock`.
