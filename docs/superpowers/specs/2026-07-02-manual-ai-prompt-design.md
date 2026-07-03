# Manual AI Prompt — Design Spec
_2026-07-02_

## Summary

Add a text entry + Send button to the bottom of the main window (between the console textbox and the footer) so the operator can send messages directly to the AI without relying on Twitch chat.

## UI Placement

Main window row layout after change:

| Row | Content |
|-----|---------|
| 0   | Header bar |
| 1   | Two-column panel (Plays + AI) |
| 2   | Console label bar + Clear button |
| 3   | Console textbox |
| **4** | **Manual prompt entry bar (new)** |
| 5   | Footer (gear button) |

The entry bar spans the full window width: a `CTkEntry` (weight=1, placeholder `"Message the AI..."`) with a `Send` button (fixed width) on the right, inside a transparent `CTkFrame`.

## Behaviour

- Submitting (button click or Enter key) does:
  1. Reads and strips the entry text; ignores empty input.
  2. Logs `[Host]: <message>` to the console.
  3. Snapshots `ai = self._ai`; if None, logs a warning and returns.
  4. Calls `ai.handle("Host", message)` directly — bypasses `_route_ai` and the `ai_enabled` toggle entirely.
  5. Clears the entry.
- TTS behaviour: `AIResponseHandler._query` reads `_var_tts_ai` from config as normal — no special casing needed.

## What is NOT changing

- `_route_ai`, `ai_enabled`, and all trigger conditions are untouched.
- `AIResponseHandler` and `TTSEngine` are untouched.
- The existing row numbers for rows 0–3 are unchanged; only row 4 (footer) shifts to row 5.

## Implementation notes

- Build the new row in a helper `_build_manual_prompt()` called from `_build_console_section()` or directly from `_build_ui()`.
- Bind `<Return>` on the entry to the same handler as the Send button.
- Thread safety: the submit handler runs on the GUI thread; `ai.handle()` is queue-safe (puts to `_q`).
