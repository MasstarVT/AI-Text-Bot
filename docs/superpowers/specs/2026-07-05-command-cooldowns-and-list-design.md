# Command Cooldowns and Auto-!commands List — Design Spec

**Date:** 2026-07-05
**Status:** Approved

---

## Overview

Two enhancements to the custom `!commands` system:

1. **Per-command cooldowns** — each command can have a configurable cooldown (in seconds) with a toggle between channel-wide (global) and per-viewer (user) mode. Commands on cooldown are silently ignored.
2. **Auto `!commands` list** — an optional built-in `!commands` response that posts a sorted, comma-separated list of all registered commands. A user-defined `!commands` entry always takes priority.

---

## Data Model

### `chat_commands` (breaking change from string values to dict values)

**Before:**
```python
chat_commands: dict[str, str]
# {"!so": "Shoutout to %user%!", "!hello": "Hey %user%!"}
```

**After:**
```python
chat_commands: dict[str, dict]
# {
#   "!so":    {"response": "Shoutout to %user%!", "cooldown": 60, "cooldown_type": "global"},
#   "!hello": {"response": "Hey %user%!",         "cooldown": 5,  "cooldown_type": "user"},
#   "!discord":{"response": "Join at discord.gg/x","cooldown": 0,  "cooldown_type": "global"},
# }
```

**Fields per command:**
- `response: str` — the chat reply (placeholder substitution applies, 500-char cap)
- `cooldown: int` — seconds; `0` means no cooldown
- `cooldown_type: str` — `"global"` (channel-wide) or `"user"` (per-viewer)

### New config key

```python
cmd_list_enabled: bool  # default False
```

Controls whether the auto-`!commands` list is active.

---

## Migration

On load in `_load_settings()` / config init, any command value that is a plain `str` is promoted:

```python
if isinstance(v, str):
    v = {"response": v, "cooldown": 0, "cooldown_type": "global"}
```

This is applied during settings load and in the `/api/settings` POST handler. Existing setups continue working with zero configuration change.

---

## Backend — Cooldown Tracking

Two in-memory dicts on `WebApp`, initialized at startup and never persisted (reset on bot restart):

```python
_cmd_global_cooldowns: dict[str, float]             # "!so" → last_fired timestamp
_cmd_user_cooldowns:   dict[tuple[str, str], float] # ("!so", "viewer1") → timestamp
```

### Cooldown check logic in `_route_chat_commands`

After finding a matching command entry:

1. Read `cooldown` and `cooldown_type` from the command dict.
2. If `cooldown == 0`: skip check, proceed to respond.
3. If `cooldown_type == "global"`:
   - Key: command word (e.g. `"!so"`)
   - If `time.time() - last_fired < cooldown`: return silently.
   - Otherwise: update `_cmd_global_cooldowns[word] = time.time()` and respond.
4. If `cooldown_type == "user"`:
   - Key: `(word, username)`
   - Same check/update logic against `_cmd_user_cooldowns`.

No additional lock needed — `_route_chat_commands` is always called from `_dispatch` on the IRC worker thread.

---

## Backend — Auto `!commands` List

When `cmd_list_enabled` is `True` and `chat_commands_enabled` is `True`:

- If the incoming word is `!commands` AND no user-defined `!commands` entry exists in `chat_commands`:
  - Build response: `"Commands: " + ", ".join(sorted(chat_commands.keys()))`
  - Truncate to 500 chars.
  - Post to IRC via `irc.say(channel, response)`.
  - Log `[Commands] {username} → !commands (auto-list)`.
- If a user-defined `!commands` entry exists, it is handled by the normal command routing before the auto-list check — the auto-list is never reached.

The auto-list carries no cooldown of its own.

---

## Frontend — UI Changes

### Commands table

Add two columns to the existing `cmd-table`:

| Command | Response | Cooldown (s) | Mode | ✕ |
|---|---|---|---|---|
| `!so` | `Shoutout to %user%!` | `60` | `Global ▾` | ✕ |
| `!hello` | `Hey %user%!` | `5` | `Per-user ▾` | ✕ |

- **Cooldown (s):** `<input type="number" min="0" value="0" style="width:60px">`
- **Mode:** `<select>` with options `Global` / `Per-user`; value stored as `"global"` / `"user"`

Both columns are always shown for simplicity (no hide-when-zero logic).

### New checkbox

Below the `+ Add command` button:

```html
<label>
  <input type="checkbox" id="s-cmd-list-enabled">
  Enable !commands list
</label>
<div class="hint">Viewers can type !commands to see all registered commands. A user-defined !commands entry takes priority.</div>
```

### JS changes

- `addCmdRow(cmd, resp, cooldown, cooldownType)` — reads/writes all four fields per row
- `getChatCommands()` — includes `cooldown` (integer) and `cooldown_type` per entry
- `openSettings()` — populates cooldown and cooldown_type per row; populates `s-cmd-list-enabled`
- `saveSettings()` — sends `cmd_list_enabled`; `getChatCommands()` already handles the rest

---

## Settings API

### `/api/settings` GET — `_SETTINGS_KEYS`

Add `"cmd_list_enabled"` to the tuple.

### `/api/settings` POST — parsing

- `cmd_list_enabled` → add to `_BOOL_KEYS`
- `chat_commands` parsing in the POST handler: after building the `cmds` dict, read `cooldown` and `cooldown_type` from each entry dict (not just `cmd`/`resp` strings).

### `_save_settings()`

Include `cmd_list_enabled` in the serialized dict.

### `_SETTINGS_DEFAULTS`

```python
"cmd_list_enabled": False,
```

---

## Config init / `_load_settings()`

```python
"cmd_list_enabled": settings.get("cmd_list_enabled", False),
"chat_commands": {
    k: (v if isinstance(v, dict) else {"response": v, "cooldown": 0, "cooldown_type": "global"})
    for k, v in settings.get("chat_commands", {}).items()
},
```

---

## Error Handling

- Malformed cooldown values (non-integer, negative) default to `0` (no cooldown).
- `cooldown_type` values other than `"global"` / `"user"` default to `"global"`.
- If `chat_commands` is empty and `cmd_list_enabled` is `True`, the auto-list response is `"Commands: "` — acceptable edge case, no special handling needed.

---

## Testing

New test file: `tests/test_command_cooldowns.py`

| Test | Covers |
|---|---|
| Global cooldown blocks repeat within window | `cooldown_type="global"`, same or different viewer |
| Global cooldown allows after window expires | timer advances past cooldown |
| Per-user cooldown blocks same viewer | `cooldown_type="user"` |
| Per-user cooldown allows different viewer during window | isolation between users |
| `cooldown=0` never blocks | always fires |
| Migration: string value promoted to dict | backward compat |
| Auto-list fires when `cmd_list_enabled=True` | correct response format |
| Auto-list suppressed when user-defined `!commands` exists | user entry wins |
| Auto-list suppressed when `cmd_list_enabled=False` | toggle works |

---

## Files Changed

| File | Change |
|---|---|
| `twitch_bot.py` | Data model migration, cooldown dicts, `_route_chat_commands` logic, config init, settings API, `_save_settings` |
| `templates/index.html` | Table columns, new checkbox, `addCmdRow`, `getChatCommands`, `openSettings`, `saveSettings` |
| `tests/test_command_cooldowns.py` | New test file (9 tests) |
