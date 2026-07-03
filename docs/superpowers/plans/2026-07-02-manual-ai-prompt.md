# Manual AI Prompt Input Bar — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a text entry + Send button between the console and the footer so the operator can send messages directly to the AI regardless of the `ai_enabled` toggle.

**Architecture:** Single method `_build_manual_prompt()` added to `TwitchBotApp` builds the entry bar at row 4, pushing the footer from row 4 to row 5. A submit handler `_send_manual_prompt()` runs on the GUI thread: logs the message, snapshots `self._ai`, and calls `ai.handle("Host", msg)` directly — bypassing `_route_ai` entirely. TTS is handled naturally by the existing `_var_tts_ai` check inside `AIResponseHandler._query`.

**Tech Stack:** Python 3, CustomTkinter (`ctk`), existing `AIResponseHandler` queue.

---

### Task 1: Add `_send_manual_prompt` handler and `_build_manual_prompt` UI method

**Files:**
- Modify: `twitch_bot.py` — add two methods to `TwitchBotApp`

- [ ] **Step 1: Add `_send_manual_prompt` method**

Insert this method in `twitch_bot.py` inside `TwitchBotApp`, after the `_clear_console` method (around line 1603):

```python
def _send_manual_prompt(self) -> None:
    msg = self._e_manual_prompt.get().strip()
    if not msg:
        return
    self._e_manual_prompt.delete(0, "end")
    self._log(f"[Host]: {msg}")
    ai = self._ai
    if not ai:
        self._log("[Host] AI not initialised — restart the app.")
        return
    ai.handle("Host", msg)
```

- [ ] **Step 2: Add `_build_manual_prompt` method**

Insert this method directly after `_send_manual_prompt`:

```python
def _build_manual_prompt(self) -> None:
    bar = ctk.CTkFrame(self, fg_color="transparent")
    bar.grid(row=4, column=0, sticky="ew", padx=10, pady=(2, 0))
    bar.grid_columnconfigure(0, weight=1)

    self._e_manual_prompt = ctk.CTkEntry(
        bar, placeholder_text="Message the AI...",
    )
    self._e_manual_prompt.grid(row=0, column=0, sticky="ew", padx=(0, 6))
    self._e_manual_prompt.bind("<Return>", lambda _: self._send_manual_prompt())

    ctk.CTkButton(
        bar, text="Send", width=80,
        command=self._send_manual_prompt,
    ).grid(row=0, column=1)
```

---

### Task 2: Wire into `_build_console_section` and shift footer to row 5

**Files:**
- Modify: `twitch_bot.py:1086-1122` — `_build_console_section`

- [ ] **Step 1: Move the footer frame from row 4 to row 5**

In `_build_console_section`, find the line:
```python
        footer.grid(row=4, column=0, sticky="ew", padx=10, pady=(6, 8))
```
Change it to:
```python
        footer.grid(row=5, column=0, sticky="ew", padx=10, pady=(6, 8))
```

- [ ] **Step 2: Call `_build_manual_prompt()` before the footer**

In `_build_console_section`, insert the call immediately before the `footer = ctk.CTkFrame(...)` line:

```python
        self._build_manual_prompt()
```

---

### Task 3: Verify manually and commit

- [ ] **Step 1: Kill any running bot and restart**

```bash
pkill -f twitch_bot.py; sleep 1
DISPLAY=:0 /home/mass/Documents/GitHub/Main/AI\ Text\ Bot/.venv/bin/python /home/mass/Documents/GitHub/Main/AI\ Text\ Bot/twitch_bot.py &
```

- [ ] **Step 2: Confirm the entry bar appears**

The main window should now show a text field labelled `Message the AI...` between the console box and the gear-button footer.

- [ ] **Step 3: Test submit with AI not connected**

Type any text and press Enter (or click Send) without connecting to Twitch. Console should show:
```
[HH:MM:SS]  [Host]: <your text>
[HH:MM:SS]  [Host] AI not initialised — restart the app.
```

- [ ] **Step 4: Test submit with AI connected**

Connect to Twitch (or ensure the services are up). Type a message and press Enter. Console should show the `[Host]: <msg>` line followed by `[AI] → <reply>`. If TTS is enabled, it should speak the reply.

- [ ] **Step 5: Test empty input is ignored**

Click Send with an empty field — nothing should happen and nothing should be logged.

- [ ] **Step 6: Commit**

```bash
cd "/home/mass/Documents/GitHub/Main/AI Text Bot"
git add twitch_bot.py
git commit -m "$(cat <<'EOF'
Add manual AI prompt input bar to console area

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```
