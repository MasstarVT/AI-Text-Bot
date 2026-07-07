# Expanded Command Placeholders Design

**Date:** 2026-07-06  
**Status:** Approved  
**Extends:** `2026-07-04-command-placeholders-design.md`

## Overview

Expand the `!command` placeholder system with four new categories: local/computed values, file-based (persistent counters and random-line pickers), Twitch API live stream data, and a web UI file manager for `data/` files.

## Placeholder Set (full, including existing)

### Local — no API, computed at call time

| Placeholder | Value | Notes |
|---|---|---|
| `%user%` | Twitch login username | existing |
| `%channel%` | Twitch channel name | existing |
| `%command%` | The command word (`!so`) | existing |
| `%args%` | Everything after the command word | existing |
| `%touser%` | First word of `%args%` with leading `@` stripped | new |
| `%time%` | Current server time — `14:32` (24 h) | new |
| `%date%` | Current server date — `July 6, 2026` | new |
| `%count%` | Times this specific command has fired this session | new |
| `%random%` | Random integer 1–100 | new |
| `%random:MIN-MAX%` | Random integer in range — e.g. `%random:1-1000%` | new |

### File-based — reads/writes files in `data/` folder

| Placeholder | Value | Notes |
|---|---|---|
| `%counter:filename%` | Read int from file → increment → save → return new value | new |
| `%randomline:filename%` | Random non-empty line from file | new |
| `%line:N:filename%` | Line N (1-indexed) from file | new |

### API-based — Twitch Helix `/streams`, cached 60 s

| Placeholder | Value | Offline fallback |
|---|---|---|
| `%game%` | Current game/category | `offline` |
| `%title%` | Stream title | `offline` |
| `%uptime%` | Time live — `2h 14m` | `offline` |
| `%viewers%` | Viewer count | `offline` |

## Architecture

### `_apply_placeholders` refactor

Replace the current static regex + dict approach with a callback-based `re.sub()` that dispatches per match type. The function signature expands to carry all needed context:

```python
def _apply_placeholders(
    response: str,
    username: str,
    channel: str,
    command: str,
    args: str,
    cmd_count: int,         # session use count for this command
    stream_info: dict,      # cached Helix data or {}
    data_dir: str,          # absolute path to data/
) -> str
```

A single regex matches all placeholder shapes:

```
%[a-zA-Z0-9_]+(?::[^%]+)?%
```

The `re.sub` callback checks each match against known patterns in order:
1. Static dict lookup for local placeholders
2. `%random%` / `%random:MIN-MAX%`
3. `%counter:filename%` — file read/increment/write
4. `%randomline:filename%` — file random line
5. `%line:N:filename%` — file specific line
6. `%game%` / `%title%` / `%uptime%` / `%viewers%` — from `stream_info`
7. Unrecognised — leave as-is

All file path arguments are sanitised: stripped of leading `/`, `..` segments rejected, characters outside `[a-zA-Z0-9_\-\.]` rejected. Returns the literal placeholder text unchanged on any path violation.

### Session use counter

`WebApp._cmd_use_counts: dict[str, int]` — keys are command words (e.g. `!deaths`). Incremented inside `_route_chat_commands` before `_apply_placeholders` is called. Never persisted; resets on restart. This is distinct from `%counter:file%` which is persistent.

### API cache

`WebApp._stream_cache: dict` and `WebApp._stream_cache_ts: float` on `WebApp`. Populated by a helper `_fetch_stream_info()` that hits `GET /helix/streams?user_login=<channel>` using the existing broadcaster token and client ID. Cache TTL is 60 seconds. If the channel is offline or credentials are missing, returns `{}` (all API placeholders render as `offline`).

`_fetch_stream_info()` is called lazily inside `_apply_placeholders` only when an API placeholder is detected. The cache and its timestamp are protected by `_config_lock`.

Uptime is calculated at substitution time from the `started_at` ISO timestamp returned by Helix.

### `data/` folder

- Created next to `twitch_bot.py` on first use (if absent).
- All file-based placeholder paths are relative to this folder.
- Path sanitisation rejects: absolute paths, `..` traversal, characters outside `[a-zA-Z0-9_\-\.]`.

`%counter:filename%` behaviour:
- File missing → treat as 0, write `1`, return `1`.
- File exists but not a valid integer → return `(invalid counter)` without overwriting.

`%randomline:filename%` behaviour:
- File missing or all lines empty → return `(file not found)`.
- Strips trailing whitespace from each line; skips blank lines.

`%line:N:filename%` behaviour:
- 1-indexed. Line 0 or negative → return `(invalid line)`.
- Line beyond file length → return `(line not found)`.

### Files tab in Settings modal

New tab added between **Commands** and **Schedule**. Renders a file manager for `data/`:

**API endpoints:**

| Method | Route | Action |
|---|---|---|
| `GET` | `/api/datafiles` | List files — returns `[{name, size}]` |
| `GET` | `/api/datafiles/<name>` | Read file content |
| `POST` | `/api/datafiles/<name>` | Create or overwrite file — body `{content}` |
| `DELETE` | `/api/datafiles/<name>` | Delete file |

All routes validate `<name>` against the same sanitisation rules as placeholder paths.

**UI (in `templates/index.html`):**

- File list table: filename, size, Edit button, Delete button.
- "New file" button opens an inline form: filename input + content textarea + Save.
- Clicking Edit loads file content into the textarea for that row (inline, no modal).
- Delete asks for confirmation before calling the DELETE endpoint.
- Changes reflect immediately (re-fetches file list after save/delete).

## Error handling

- Unknown placeholders left as-is (existing behaviour, unchanged).
- API fetch failure → `stream_info = {}` → API placeholders render as `offline`; error logged to console.
- File I/O errors → placeholder renders as `(error)` and error is logged; bot continues.

## Out of scope

- No placeholders in Twitch Plays commands, scheduler messages, or thanks responses.
- No nested placeholders.
- No file editing in the placeholder itself (read-only except `%counter%`).
- No sub-folder creation in the Files UI — flat `data/` only.
