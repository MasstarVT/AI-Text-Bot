# Discord Integration Design

**Date:** 2026-07-02
**Status:** Approved

## Overview

Add a `DiscordClient` class to `twitch_bot.py` that connects the bot to a Discord channel, receives messages, routes them through the existing `AIResponseHandler`, and posts AI replies back to Discord. Twitch and Discord run independently — either or both can be connected at once.

## Architecture

A new `DiscordClient` class lives in `twitch_bot.py` alongside `TwitchIRCClient`, `AIResponseHandler`, `TTSEngine`, and `GameInputController`. It uses `discord.py` and runs its own `asyncio` event loop on a background daemon thread.

`TwitchBotApp` gains a `_discord: DiscordClient | None` service handle managed independently from `_irc`.

### New class: `DiscordClient`

Responsibilities:
- Start a `discord.py` `commands.Bot` (or `discord.Client`) on a dedicated daemon thread with its own `asyncio` event loop
- Listen to `on_message` events in the configured channel
- Apply trigger-mode filtering (all messages / @mention only / @mention + replies / all + mentions + replies)
- Call `AIResponseHandler.handle(username, message, reply_cb)` with a reply callback
- Send the AI reply back to Discord via `asyncio.run_coroutine_threadsafe(channel.send(reply), loop)`
- Expose `connect()` and `disconnect()` matching the `TwitchIRCClient` interface

Constructor signature:
```python
DiscordClient(get_config, log, ai_handler: AIResponseHandler)
```
`get_config` returns a dict with keys: `discord_token`, `discord_channel_id`, `discord_trigger`, `discord_prompt` (empty string means use shared prompt).

### Changes to `AIResponseHandler`

`handle()` gains an optional `reply_cb` parameter:
```python
def handle(self, username: str, message: str, reply_cb=None) -> None
```
The `(username, message, reply_cb)` tuple is enqueued. After a successful AI response, if `reply_cb` is not None, it is called with the reply text. Existing Twitch callers pass no `reply_cb` — no behaviour change.

The `reply_cb` for Discord is a callable that schedules `channel.send(reply)` on the Discord asyncio loop via `run_coroutine_threadsafe`.

## UI / Configuration

### Connection Settings window — new Discord section

Added below the existing LLM/Piper sections:

| Field | Widget | `.env` key |
|---|---|---|
| Bot Token | `CTkEntry` (show=`*`) | `DISCORD_TOKEN` |
| Channel ID | `CTkEntry` | `DISCORD_CHANNEL_ID` |
| Trigger mode | `CTkComboBox` | `DISCORD_TRIGGER` |
| Use shared prompt | `CTkCheckBox` | `DISCORD_USE_SHARED_PROMPT` |
| Discord system prompt | `CTkTextbox` (shown when checkbox off) | `DISCORD_PROMPT` |
| Connect / Disconnect | `CTkButton` pair | — |

Trigger mode options: `All messages`, `@mention only`, `@mention + replies`, `All messages + mentions + replies`.

### Main window header

A second status label ("Discord: ●") appears next to the existing Twitch status label, coloured green (connected) or red (disconnected/off).

### `.env` persistence

Bot Token and all Discord fields are written to `.env` by `_save_env()` on Discord connect (same pattern as Twitch credentials). `_load_env()` pre-fills all Discord fields on startup.

New `.env` keys: `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_TRIGGER`, `DISCORD_USE_SHARED_PROMPT`, `DISCORD_PROMPT`.

## Data Flow

```
Discord message arrives
  → DiscordClient.on_message() [asyncio event on daemon thread]
      → ignore if wrong channel or trigger-mode filter not met
      → resolve system prompt (shared or Discord-specific)
      → build reply_cb = lambda text: run_coroutine_threadsafe(channel.send(text), loop)
      → AIResponseHandler.handle(username, message, reply_cb)
          → AI worker thread POSTs to configured LLM provider
          → logs "[Discord AI] → <reply>" to console via _log_queue
          → calls reply_cb(reply) → Discord message sent back to channel
```

## Error Handling

- Invalid token or missing channel ID: logged to console, `_discord` remains None, status label stays red.
- Channel not found (wrong ID): logged as `[Discord] Channel <id> not found`, bot stays connected but silent.
- AI errors propagate through the existing `AIResponseHandler` error paths; Discord reply callback is not called on error (so no broken message is posted).
- `discord.py` auto-reconnects on network drops — no manual reconnect loop needed.

## Dependencies

Add `discord.py` to `requirements.txt`:
```
discord.py>=2.3.0
```

No other new dependencies.

## Testing Checklist

- [ ] Discord bot token + channel ID entered in Settings, saved to `.env`, pre-filled on restart
- [ ] "All messages" trigger: every message in the channel gets an AI reply
- [ ] "@mention only" trigger: only messages that @mention the bot get a reply
- [ ] "@mention + replies" trigger: mentions and replies to bot messages get a reply
- [ ] "All messages + mentions + replies" trigger: all messages get a reply
- [ ] Discord-specific system prompt: enter a custom prompt, confirm AI uses it
- [ ] Shared prompt toggle: switching to shared uses the Twitch panel's prompt
- [ ] AI reply posted back to Discord channel
- [ ] Console shows `[Discord] <user>: <message>` and `[Discord AI] → <reply>`
- [ ] Twitch and Discord can run simultaneously without interference
- [ ] Disconnecting Discord does not affect Twitch connection
- [ ] `.env` keys persist across app restarts
