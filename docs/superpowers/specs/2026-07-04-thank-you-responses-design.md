# Thank-You Responses — Design Spec
_Date: 2026-07-04_

## Overview

The bot will detect Twitch channel events (new subs, resubs, gifted subs, mystery gift subs, bits cheers, raids) and generate AI-powered thank-you messages in response. Replies are delivered to Twitch chat, spoken via TTS, or both — each independently toggled. A separate system prompt controls the thank-you AI personality.

---

## 1. IRC Layer (`TwitchIRCClient`)

### `on_event` callback

Add `on_event: callable | None = None` attribute. Called as `on_event(event_type: str, username: str, extra: dict)`.

### USERNOTICE parsing in `_handle`

After the existing `PRIVMSG` block, parse `USERNOTICE` messages:

```
msg-id       → event_type
display-name → username (gifter / raider / subscriber)
```

Event types and extra fields extracted from IRCv3 tags:

| `msg-id`         | `event_type`   | Extra keys                                               |
|------------------|----------------|----------------------------------------------------------|
| `sub`            | `sub`          | `plan` (Tier 1/2/3)                                      |
| `resub`          | `resub`        | `months` (cumulative), `streak` (streak months), `plan`  |
| `subgift`        | `subgift`      | `recipient` (display name), `plan`                       |
| `submysterygift` | `mysterygift`  | `count` (number of gifts)                                |
| `raid`           | `raid`         | `viewers` (viewer count)                                 |

Bits cheers already arrive via `PRIVMSG` with a `bits` tag and fire `on_message` — handled separately in the app layer (no USERNOTICE parsing needed).

### `send_chat(msg: str)` method

New method on `TwitchIRCClient`. Sends `PRIVMSG #{channel} :{msg}\r\n` to the socket. Snapshots `self._sock` to a local before use (TOCTOU-safe). Silently returns if socket is `None`.

---

## 2. App Layer (`TwitchBotApp`)

### `_handle_event(event_type, username, extra)`

Wired to `self._sock_client.on_event` at connect time.

Logic:

1. Return early if `thanks_enabled` is `False` in config.
2. Check per-event toggle — return if this event type is disabled.
3. Check `AIResponseHandler` is connected (`self._ai` is not `None`).
4. Build user-turn string describing the event (see templates below).
5. Call `self._ai.handle(username, event_msg, system_prompt=<thanks_prompt>, use_tts=<thanks_tts>)`.
6. The reply callback: if `thanks_chat` is enabled, call `sock.send_chat(reply)` (TOCTOU-safe snapshot of `self._sock`).

### Bits cheer integration

`_dispatch` already fires for bits cheers via `on_message`. Add a check: if `bits > 0` and `thanks_enabled` and `thanks_bits`, call `_handle_event("bits", username, {"bits": bits})` after the existing `_route_ai` call.

**Double-fire note:** If both `trigger_bits` (existing AI trigger) and `thanks_bits` are enabled, a single cheer fires the AI twice — once for the regular AI response and once for the thank-you. This is intentional and acceptable; the two calls use different system prompts. If the user wants to avoid this, they should disable `trigger_bits` when `thanks_bits` is on, or vice versa. No automatic deduplication is implemented.

### Event message templates

| Event        | User-turn message                                                                              |
|--------------|-----------------------------------------------------------------------------------------------|
| `sub`        | `[EVENT] {username} just subscribed! Thank them warmly.`                                      |
| `resub`      | `[EVENT] {username} resubscribed for {months} months ({streak} month streak)! Thank them.`    |
| `subgift`    | `[EVENT] {username} gifted a sub to {recipient}! Thank {username}.`                           |
| `mysterygift`| `[EVENT] {username} gifted {count} subs to the community! Thank them.`                        |
| `raid`       | `[EVENT] {username} raided with {viewers} viewers! Welcome them and their community.`          |
| `bits`       | `[EVENT] {username} cheered {bits} bits! Thank them.`                                         |

### `AIResponseHandler.handle()` signature extension

Add optional `system_prompt: str | None = None` parameter. If provided, overrides the prompt from `get_config()` for this single call. This keeps thank-you AI prompt separate from the main AI prompt without touching other call sites.

### Reply callback

The AI `reply_cb` for thank-you events:

```python
def _thanks_reply_cb(reply: str) -> None:
    if thanks_chat:
        sock = self._sock
        if sock:
            sock.send_chat(reply)
    self._log(f"[Thanks] {reply}")
```

TTS is handled by `use_tts` parameter passed to `ai.handle()`.

---

## 3. Configuration

### In-memory config dict (new keys)

| Key              | Type   | Default | Description                         |
|------------------|--------|---------|-------------------------------------|
| `thanks_enabled` | bool   | False   | Master enable toggle                |
| `thanks_sub`     | bool   | True    | Fire on new sub                     |
| `thanks_resub`   | bool   | True    | Fire on resub                       |
| `thanks_gift`    | bool   | True    | Fire on single gifted sub           |
| `thanks_mystery` | bool   | True    | Fire on mystery gift subs           |
| `thanks_bits`    | bool   | False   | Fire on bits cheer                  |
| `thanks_raid`    | bool   | True    | Fire on raid                        |
| `thanks_chat`    | bool   | True    | Post reply to Twitch chat           |
| `thanks_tts`     | bool   | True    | Speak reply via TTS                 |
| `thanks_prompt`  | str    | (see §4)| System prompt for thank-you AI      |

### `.env` persistence

New keys saved/loaded alongside existing ones:

```
THANKS_ENABLED, THANKS_SUB, THANKS_RESUB, THANKS_GIFT,
THANKS_MYSTERY, THANKS_BITS, THANKS_RAID, THANKS_CHAT, THANKS_TTS, THANKS_PROMPT
```

Boolean keys stored as `"True"` / `"False"` strings, same as existing pattern.

---

## 4. UI (Connection Settings → "Thank-You" tab)

New tab in the existing `_settings_win` CTkToplevel, built by `_build_thanks_tab()`.

### Layout

```
[✓] Enable thank-you responses

Events
  [✓] New sub        [✓] Resub
  [✓] Gifted sub     [✓] Mystery gifts
  [  ] Bits cheer    [✓] Raid

Delivery
  [✓] Post to Twitch chat    [✓] Speak via TTS

Thank-you prompt
  ┌──────────────────────────────────────────────────────────────────┐
  │ You are a friendly Twitch bot. Thank viewers warmly and briefly  │
  │ for their support.                                               │
  └──────────────────────────────────────────────────────────────────┘
  [Save Prompt]  [Load Prompt]
```

### Default system prompt

```
You are a friendly Twitch streamer's bot. When a viewer subs, resubs, gifts subs,
cheers bits, or raids, respond with a warm, brief, personalized thank-you message
that fits naturally in Twitch chat. Keep it under two sentences. Do not use hashtags.
```

### Thread-safety

`thanks_prompt` textbox follows the same `_thanks_prompt_cache` / `_thanks_prompt_lock` pattern as the main `_prompt_cache` — synced every 80 ms in `_poll_logs`. Workers read the cache, not the CTkTextbox directly.

---

## 5. Data flow summary

```
IRC thread → TwitchIRCClient._handle() → USERNOTICE detected
  → on_event(event_type, username, extra)
    → TwitchBotApp._handle_event()
      → checks enabled / per-event toggle
      → builds event_msg string
      → AIResponseHandler.handle(username, event_msg, system_prompt=thanks_prompt, use_tts=thanks_tts)
        → LLM generates reply
          → reply_cb(reply)
            → send_chat(reply)  [if thanks_chat]
            → TTSEngine.enqueue(reply)  [if thanks_tts, handled by use_tts flag]
            → _log(f"[Thanks] {reply}")
```

---

## 6. Out of scope

- `subgift` upgrades (`giftpaidupgrade`) — rare, skip for now
- Prime sub detection (can be added later via `msg-param-sub-plan = Prime`)
- Per-event custom prompts (single shared thank-you prompt is sufficient)
- Rate limiting (Twitch allows 20 messages/30s for normal bots; not a concern for low-frequency events)
