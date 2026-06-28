# Twitch Interactive Bot

A customizable Twitch stream interaction tool with Twitch Plays game control, AI chat responses via a local LLM, and Piper TTS voice output. Inspired by DougDoug's stream setups.

## Features

- **Twitch Plays** — map chat commands (e.g. `!jump`) to keyboard keys with configurable hold durations
- **AI Chat Responses** — connects to a local LLM (Ollama / LM Studio) and responds to chat every N messages or on @mentions
- **Text-to-Speech** — speaks AI replies aloud using [Piper TTS](https://github.com/rhasspy/piper) + pygame
- **Flexible AI Triggers** — fire the AI on every N messages, @mentions, bits cheers (with a minimum threshold), or specific channel point redemptions
- **System Prompt Manager** — save and load named system prompts from the `prompts/` folder
- **Dark GUI** — built with [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter)

## Requirements

- Python 3.10+
- A running local LLM server (Ollama or LM Studio)
- Piper TTS binary + a `.onnx` voice model (optional, for audio)

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

1. **Connection tab** — enter your Twitch channel, bot username, and OAuth token (`oauth:...`). Configure the LLM endpoint/model and optionally Piper TTS paths, then click **Connect**.
2. **Twitch Plays tab** — add `!command → key` mappings and toggle game input on/off.
3. **AI Interaction tab** — enable AI responses, configure trigger conditions (every N messages, @mentions, bits, channel points), edit the system prompt, and save/load prompts from the `prompts/` folder.
4. **Console tab** — live log output from all threads.

### Getting a Twitch OAuth token

Generate one at <https://twitchapps.com/tmi/> — it will look like `oauth:xxxxxxxxxxxxxxxx`.

### Local LLM setup

- **Ollama**: `ollama serve` + `ollama pull llama3` → endpoint `http://localhost:11434/v1/chat/completions`
- **LM Studio**: start the local server → endpoint `http://localhost:1234/v1/chat/completions`

## Project structure

```
AI Text Bot/
├── twitch_bot.py      # Main application
├── requirements.txt
├── prompts/           # Saved system prompts (created automatically)
└── README.md
```

## Threading model

| Thread | Role |
|---|---|
| Main | GUI (CustomTkinter mainloop) |
| IRC | Raw TCP socket reader, auto-reconnects |
| AI | HTTP requests to local LLM |
| TTS | Piper subprocess + pygame playback |
| Input | Short-lived threads per key press |

Cross-thread communication is done entirely through `queue.Queue` objects; no GUI calls are made from worker threads.
