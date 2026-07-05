# Command Response Placeholders

Use these placeholders in your custom `!command` response text. They are replaced at runtime when the command fires in chat.

| Placeholder | Value | Example output |
|---|---|---|
| `%user%` | Twitch username of the person who typed the command | `streamer42` |
| `%channel%` | Twitch channel name | `mychannel` |
| `%command%` | The command word itself | `!so` |
| `%args%` | Everything typed after the command word (empty if nothing) | `@StreamerName` |

## Examples

**Shoutout command**

Command: `!so`
Response: `Go check out %args%, they're an awesome streamer! PogChamp`
User types: `!so @StreamerName`
Bot posts: `Go check out @StreamerName — they're an awesome streamer! PogChamp`

---

**Welcome command**

Command: `!welcome`
Response: `Welcome to %channel%, %user%! Glad you're here!`
User types: `!welcome`
Bot posts: `Welcome to mychannel, viewer42! Glad you're here!`

---

**Notes**

- Placeholders are case-sensitive — use lowercase exactly as shown.
- Unknown or misspelled placeholders (e.g. `%usr%`) are left as-is in the output.
- `%args%` is an empty string if the user types the command with no arguments.
- The full response (after substitution) is still capped at 500 characters before posting.
- `%command%` always expands to the lowercase form of the command (e.g. `!so` even if typed as `!SO`).
