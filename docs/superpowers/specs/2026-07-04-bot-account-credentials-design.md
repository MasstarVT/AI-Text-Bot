---
name: bot-account-credentials
description: Split Twitch credentials into a dedicated bot account (IRC) and an optional broadcaster account (EventSub follow events only)
metadata:
  type: project
---

# Bot Account Credentials

## Goal

Allow the bot to connect to Twitch IRC using a dedicated bot account rather than the streamer's account, while keeping the existing broadcaster credentials available for EventSub (follow event detection). The broadcaster fields are optional — users who don't need follow events only configure the bot account.

## Credential split

| Field | Key | Used by |
|---|---|---|
| Bot Username | `bot_username` / `BOT_USERNAME` | IRC login (read + write chat) |
| Bot OAuth Token | `bot_token` / `BOT_TOKEN` | IRC login |
| Broadcaster Username | `twitch_username` / `TWITCH_USERNAME` | EventSub only (optional) |
| Broadcaster Token | `twitch_token` / `TWITCH_TOKEN` | EventSub only (optional) |
| Channel | `twitch_channel` / `TWITCH_CHANNEL` | IRC join target + EventSub channel |

`_get_irc_creds()` returns `bot_username` + `bot_token` + `twitch_channel`.

`_get_eventsub_creds()` is unchanged — continues to return `twitch_token` + `twitch_client_id` + `twitch_channel`.

If `twitch_token` is blank at connect time, EventSub connection is skipped with a log line: `[EventSub] Broadcaster token not set — skipping follow event detection`.

If `bot_username` or `bot_token` is blank at connect time, the IRC connect is rejected with a clear error before attempting any socket connection.

## Bot identity & self-filter

- `_route_ai` mention detection: reads `bot_username` instead of `twitch_username` for `bot_user`.
- `_dispatch` self-filter: if the incoming IRC username matches `bot_username` (case-insensitive), drop the message before any routing (AI, commands, plays, history). Prevents the bot from reacting to its own chat output.

## `.env` persistence

New keys written by `_save_env()` and read by `_load_env()`:

```
BOT_USERNAME=<bot account login name>
BOT_TOKEN=oauth:<bot oauth token>
```

Existing `TWITCH_USERNAME` / `TWITCH_TOKEN` entries remain in `.env` for EventSub. On load, if `BOT_USERNAME` is missing or empty and `TWITCH_USERNAME` is set, the bot fields are pre-filled from the streamer fields as a one-time migration so existing setups connect without reconfiguration. The migrated values are written back on the next `_save_env()` call (i.e. next Connect press).

## UI — Connection Settings (Twitch tab)

New fields added to the Twitch tab:

- **Bot Username** — entry field, required
- **Bot OAuth Token** — entry field, required

Existing fields relabeled:

- ~~Username~~ → **Broadcaster Username**
- ~~Token~~ → **Broadcaster Token**

A note `(optional — follow events only)` appears below the Broadcaster Token field.

Connect button validation checks `bot_username` + `bot_token` (not broadcaster fields). Broadcaster fields may be left blank.

Field order in the tab: Channel → Bot Username → Bot OAuth Token → (separator) → Broadcaster Username → Broadcaster Token → Client ID.

## Out of scope

- Discord credentials are unaffected.
- EventSub subscription scopes are not changed.
- No UI changes to any tab other than the Twitch tab.
