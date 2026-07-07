# Phase 1: Role System, Counter Commands, Quote System

**Date:** 2026-07-07
**Status:** Approved

---

## Overview

Three interconnected features added to the existing dispatch pipeline:

1. **Role system** — Twitch native roles auto-detected from IRC badges; custom roles assigned via chat commands or web UI; role sets passed to all routing functions.
2. **Counter commands** — Streamer-defined persistent counters (`!deaths`, `!wins`, etc.) with role-gated editing.
3. **Quote system** — Role-gated quote submission, random/indexed retrieval, mod deletion.

All new data lives in `data/` using existing file I/O patterns. No new dependencies.

---

## 1. Role System

### Storage

`data/roles.json` — maps custom role names to lists of lowercase usernames:

```json
{
  "trusted": ["viewer1", "viewer2"],
  "artist": ["viewer3"]
}
```

File is created empty on first run if missing.

### Twitch Native Roles

Parsed from the `badges` IRCv3 tag on every incoming message. Recognized values: `broadcaster`, `moderator`, `vip`, `subscriber`. No storage required — evaluated per message.

### Role Set Construction

Every message that enters `_dispatch` builds a `user_roles: set[str]` for the sender:

1. Start with `{"everyone"}`
2. Parse `badges` tag → add matching native roles
3. Load `data/roles.json` → add any custom roles the user belongs to

This set is passed to `_route_counters`, `_route_quotes`, and `_route_chat_commands`.

### Chat Commands

All three commands require the sender to be `moderator` or `broadcaster` (checked against `user_roles`):

| Command | Effect |
|---|---|
| `!addrole <user> <role>` | Grant custom role to user; writes to `data/roles.json` |
| `!removerole <user> <role>` | Revoke custom role; writes to `data/roles.json` |
| `!roles <user>` | Posts user's full role set to chat |

Role names are stored and matched lowercase. `!addrole` creates the role key if it doesn't exist.

### Command Gating

Each entry in `chat_commands` gains an `"allowed_roles": []` field. Empty list means everyone can use the command. `_route_chat_commands` checks `user_roles & set(allowed_roles)` — if the intersection is empty and `allowed_roles` is non-empty, the command is silently ignored for that user.

**Migration:** On settings load, any `chat_commands` entry missing `allowed_roles` gets `[]` injected automatically. No user action required.

### Web UI

New **Roles** tab in the Settings modal:

- Text input to create a new custom role name
- Per-role expandable section listing members with a remove button per member
- "Add member" input per role
- Delete role button (removes role and all its assignments)

### File Locking

`data/roles.json` reads/writes use the same `threading.Lock` pattern as the existing data file system. `!addrole` and `!removerole` acquire the lock, load, mutate, and save atomically.

---

## 2. Counter Commands

### Storage

`data/counters.json` — maps counter name to a config object:

```json
{
  "deaths": {"value": 0, "display": "Deaths: {value}", "edit_roles": ["moderator", "broadcaster"]},
  "wins":   {"value": 0, "display": "Wins: {value}",   "edit_roles": ["moderator", "broadcaster"]},
  "losses": {"value": 0, "display": "Losses: {value}", "edit_roles": ["moderator", "broadcaster"]}
}
```

File is written with these three presets on first run if missing.

### Chat Commands

| Command | Who | Effect |
|---|---|---|
| `!<counter>` | anyone | Posts display string with current value |
| `!<counter> +1` | `edit_roles` | Increment by 1, post updated value |
| `!<counter> -1` | `edit_roles` | Decrement by 1 (floor 0), post updated value |
| `!<counter> set <n>` | moderator/broadcaster | Set to exact integer value |
| `!<counter> reset` | moderator/broadcaster | Reset to 0 |
| `!addcounter <name>` | moderator/broadcaster | Create counter with default display/edit_roles |
| `!delcounter <name>` | moderator/broadcaster | Delete counter |

`+1`/`-1` check `user_roles & set(edit_roles)`. `set`/`reset`/`addcounter`/`delcounter` require `moderator` or `broadcaster` regardless of `edit_roles`.

Counter names are stored and matched lowercase. `!<counter>` matches only if the word exactly equals a known counter name — no conflict with custom `!commands`.

Value changes are written to `data/counters.json` immediately after each mutation.

### Routing Priority

`_route_counters` is called before `_route_chat_commands` in `_dispatch`. A counter name takes priority over an identically-named custom command.

### Web UI

New **Counters** tab in Settings modal:

- Table of all counters: name, current value (editable inline), display format, edit roles (multi-select)
- "Add counter" row at the bottom: name + display format inputs
- Delete button per row
- Changes saved via API call; counter file updated immediately

### Display Format

`{value}` in the display string is replaced with the current integer. Default: `"<Name>: {value}"` where `<Name>` is the counter name title-cased.

---

## 3. Quote System

### Storage

`data/quotes.json` — list of quote objects, ordered by insertion:

```json
[
  {
    "id": 1,
    "text": "That was absolutely insane!",
    "author": "streamername",
    "added_by": "modname",
    "timestamp": "2026-07-07T12:00:00"
  }
]
```

`id` values are sequential starting at 1 and are never reused. Gaps after deletion are acceptable.

### Chat Commands

| Command | Who | Effect |
|---|---|---|
| `!quote` | anyone | Post a random quote |
| `!quote <id>` | anyone | Post specific quote by ID |
| `!quotecount` | anyone | Post total number of quotes |
| `!addquote <text>` | configured role | Add quote; `author` = current channel name, `added_by` = sender |
| `!delquote <id>` | moderator/broadcaster | Delete quote by ID |

Quote display format: `[#<id>] <text> — <author> (<YYYY-MM-DD>)`

`!addquote` role is configurable in Settings (defaults to `moderator`). The check uses `user_roles & {configured_role, "broadcaster"}`.

`!quote` with a non-existent ID posts: `"Quote #<id> not found."`

### Routing

`_route_quotes` is called from `_dispatch` after `_route_counters` and before `_route_chat_commands`.

### Web UI

New **Quotes** tab in Settings modal:

- Search input (filters by text or author)
- Scrollable table: ID, text, author, added_by, date, delete button
- Dropdown at top to configure which role can use `!addquote`

---

## 4. Dispatch Wiring

Updated `_dispatch` call order:

```
1. Ignore check (existing)
2. Bot self-filter (existing)
3. Build user_roles set  ← NEW
4. _route_counters(username, message, user_roles)  ← NEW
5. _route_quotes(username, message, user_roles)  ← NEW
6. _route_chat_commands(username, message, user_roles)  ← gains role param
7. _route_plays(message)  (existing)
8. _route_ai(username, message)  (existing)
```

`_handle_event` (USERNOTICE path) does not receive `user_roles` — thank-you responses are not role-gated.

---

## 5. API Routes

New Flask routes to support web UI CRUD:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/roles` | Return all custom roles and members |
| POST | `/api/roles/<role>/members` | Add member to role |
| DELETE | `/api/roles/<role>/members/<user>` | Remove member from role |
| DELETE | `/api/roles/<role>` | Delete entire role |
| GET | `/api/counters` | Return all counters |
| POST | `/api/counters` | Create counter |
| PATCH | `/api/counters/<name>` | Update value/display/edit_roles |
| DELETE | `/api/counters/<name>` | Delete counter |
| GET | `/api/quotes` | Return all quotes (optional `?q=` search) |
| DELETE | `/api/quotes/<id>` | Delete quote by ID |

Quote creation happens via chat only (`!addquote`), not through the web UI.

---

## Out of Scope

- Role hierarchy / inheritance (roles are flat; precedence is union-based)
- Per-counter min value (floor is always 0)
- Quote editing after submission
- Exporting quotes or counters to CSV
