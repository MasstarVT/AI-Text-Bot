# Twitch Interactive Bot

A customizable Twitch stream interaction tool with Twitch Plays game control, AI chat responses via a local LLM, Piper TTS voice output, and Discord bot integration. Inspired by DougDoug's stream setups.

## Features

- **Twitch Plays** — map chat commands (e.g. `!jump`) to keyboard keys with configurable hold durations
- **AI Chat Responses** — connects to a local LLM (Ollama / LM Studio / OpenAI / Grok / Gemini / Claude) and responds to chat every N messages or on @mentions
- **Text-to-Speech** — speaks AI replies aloud using [Piper TTS](https://github.com/rhasspy/piper) + pygame
- **Flexible AI Triggers** — fire the AI on every N messages, @mentions, bits cheers (with a minimum threshold), or specific channel point redemptions
- **Discord Bot** — connect a Discord bot to a channel; the AI responds to messages using the same LLM with configurable trigger modes (@mention only, all messages, etc.)
- **System Prompt Manager** — save and load named system prompts from the `prompts/` folder; Discord can use a shared or separate prompt
- **Dark GUI** — built with [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter)

## Requirements

- Python 3.10+
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

The UI has no tabs — everything is visible at a glance:

1. **Header bar** — click **Connect** after filling in credentials, or **Disconnect** to stop. Status indicators show the live Twitch and Discord connection states.
2. **Left panel (Twitch Plays)** — add `!command → key` mappings and toggle game input on/off.
3. **Right panel (AI Interaction)** — enable AI responses, configure trigger conditions (every N messages, @mentions, bits, channel points), edit the system prompt, and save/load prompts from the `prompts/` folder.
4. **Console** — pinned to the bottom; live log output from all threads.
5. **⚙ Connection Settings** — click the gear button in the footer to open the connection settings popup (Twitch credentials, LLM endpoint/model, Piper TTS paths, and Discord bot settings).

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
├── .env               # Saved connection settings (git-ignored, created on first Connect)
├── settings.json      # UI state (AI toggles, trigger settings, last prompt — auto-saved every 10s)
├── piper/             # Bundled Piper TTS binary (git-ignored)
│   └── piper          # The executable — auto-detected and pre-filled in the UI
├── Voices/            # Voice model files — .onnx binaries are git-ignored; .json configs committed
├── prompts/           # Saved system prompts (created automatically)
└── README.md
```

> **Note:** `.env` is listed in `.gitignore` so your Twitch token and credentials are never committed.

## Threading model

| Thread | Role |
|---|---|
| Main | GUI (CustomTkinter mainloop) |
| IRC | Raw TCP socket reader, auto-reconnects |
| AI | HTTP requests to local LLM |
| TTS | Piper subprocess + pygame playback |
| Discord | `discord.py` asyncio event loop (daemon thread) |
| Input | Short-lived threads per key press |

Cross-thread communication is done entirely through `queue.Queue` objects; no GUI calls are made from worker threads.
