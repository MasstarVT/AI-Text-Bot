# Command Response Placeholders

Use these placeholders in your custom `!command` response text. They are replaced at runtime when the command fires in chat.

---

## Local Placeholders

No API or files needed — computed instantly at call time.

| Placeholder | Value | Example output |
| --- | --- | --- |
| `%user%` | Twitch username of the person who typed the command | `streamer42` |
| `%channel%` | Twitch channel name | `mychannel` |
| `%command%` | The command word itself | `!so` |
| `%args%` | Everything typed after the command word (empty if nothing) | `@StreamerName` |
| `%touser%` | First word of `%args%` with `@` stripped — useful for shoutouts | `StreamerName` |
| `%time%` | Current server time (24-hour) | `14:32` |
| `%date%` | Current server date | `July 6, 2026` |
| `%count%` | How many times this command has fired this session (resets on bot restart) | `7` |
| `%random%` | Random integer from 1 to 100 | `42` |
| `%random:MIN-MAX%` | Random integer in your range | `%random:1-1000%` → `537` |

---

## File-Based Placeholders

Files live in the `data/` folder next to `twitch_bot.py`. Manage them via **Settings → Files**.

Filenames must use only letters, numbers, underscores, hyphens, and dots (`a-z A-Z 0-9 _ - .`).

| Placeholder | Value |
| --- | --- |
| `%counter:filename%` | Reads a number from the file, adds 1, saves it back, returns the new value. Creates the file at 0 if missing. |
| `%randomline:filename%` | Picks a random non-empty line from the file. Returns `(file not found)` if the file is missing, `(empty file)` if all lines are blank. |
| `%line:N:filename%` | Returns line N (1-indexed) from the file. Returns `(line not found)` if N is out of range. |

### Examples

**Death counter** — increments every time someone runs `!deaths`:

Command: `!deaths`
Response: `The streamer has died %counter:deaths.txt% times today.`

---

**Fun fact** — picks a random line from `data/facts.txt`:

Command: `!fact`
Response: `Fun fact: %randomline:facts.txt%`

---

**Specific quote** — always returns line 3 of `data/quotes.txt`:

Command: `!quote`
Response: `"%line:3:quotes.txt%"`

---

## API Placeholders

Pulls live data from Twitch. Requires your Broadcaster Token and Client ID to be configured. Cached for 60 seconds. Returns `offline` if the stream is not live or credentials are missing.

| Placeholder | Value |
| --- | --- |
| `%game%` | Current game or category |
| `%title%` | Stream title |
| `%uptime%` | How long the stream has been live — `2h 14m` |
| `%viewers%` | Current viewer count |

---

## Notes

- Placeholders are case-sensitive — use lowercase exactly as shown.
- Unknown or misspelled placeholders (e.g. `%usr%`) are left as-is in the output.
- `%args%` is an empty string if the user types the command with no arguments.
- `%touser%` is an empty string if there are no args.
- The full response (after substitution) is capped at 500 characters before posting.
- `%command%` always expands to the lowercase form of the command (e.g. `!so` even if typed as `!SO`).
- `%count%` tracks uses in the current bot session only. Use `%counter:file%` for an all-time persistent count.
- File-based placeholder paths are relative to the `data/` folder — you cannot reference files outside it.
