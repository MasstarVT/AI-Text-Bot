# Plays Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add named preset save/load/new functionality to the Twitch Input Plays panel, storing each preset as a `.json` file in a `plays_presets/` directory.

**Architecture:** A `plays_presets/` directory (parallel to `prompts/`) holds one `.json` file per preset containing the full `command_map` dict. A preset bar (new UI row in `_build_plays`) exposes a `CTkComboBox` for selecting/naming presets and a Save button; selecting an existing name immediately replaces the active command map. All logic lives in `twitch_bot.py`.

**Tech Stack:** Python 3, customtkinter, json stdlib

---

## File Map

| File | Change |
|---|---|
| `twitch_bot.py` | All changes — init, `_build_plays`, 5 new methods |

---

### Task 1: Initialise the presets directory

**Files:**
- Modify: `twitch_bot.py:581-584`

- [ ] **Step 1: Add `_presets_dir` path and makedirs call**

In `TwitchBotApp.__init__`, immediately after line 584 (`os.makedirs(self._prompts_dir, exist_ok=True)`), add:

```python
        self._presets_dir = os.path.join(_here, "plays_presets")
        os.makedirs(self._presets_dir, exist_ok=True)
```

- [ ] **Step 2: Verify app still starts**

```bash
DISPLAY=:0 .venv/bin/python twitch_bot.py
```

Expected: app opens normally, a new `plays_presets/` directory appears next to the script. No console errors.

- [ ] **Step 3: Commit**

```bash
git add twitch_bot.py
git commit -m "feat: initialise plays_presets directory on startup"
```

---

### Task 2: Add preset helper methods

**Files:**
- Modify: `twitch_bot.py` — add methods in the Command-mapping UI section (after `_refresh_plays`, before the Settings persistence section)

- [ ] **Step 1: Add `_list_presets` and `_preset_values`**

After the `_refresh_plays` method (around line 1354), insert:

```python
    def _list_presets(self) -> list[str]:
        if not os.path.isdir(self._presets_dir):
            return []
        return sorted(f[:-5] for f in os.listdir(self._presets_dir) if f.endswith(".json"))

    def _preset_values(self) -> list[str]:
        return ["+ New Preset"] + self._list_presets()
```

- [ ] **Step 2: Add `_on_preset_selected`**

Immediately after the two methods above, add:

```python
    def _on_preset_selected(self, name: str) -> None:
        if name == "+ New Preset":
            self._new_preset()
            return
        path = os.path.join(self._presets_dir, f"{name}.json")
        if not os.path.exists(path):
            self._log(f"[Presets] File not found: {name}.json")
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self._log(f"[Presets] Failed to load '{name}': {e}")
            return
        self.command_map = {k: v for k, v in data.items()}
        self._refresh_plays()
        self._log(f"[Presets] Loaded: {name}")
```

- [ ] **Step 3: Add `_save_preset`**

```python
    def _save_preset(self) -> None:
        name = self._preset_combo.get().strip()
        if not name or name == "+ New Preset":
            self._log("[Presets] Select or type a preset name first.")
            return
        safe = re.sub(r'[^\w\s\-]', '', name).strip()
        if not safe:
            self._log("[Presets] Invalid name — use letters, numbers, spaces, or dashes.")
            return
        path = os.path.join(self._presets_dir, f"{safe}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.command_map, f, indent=2)
        self._preset_combo.configure(values=self._preset_values())
        self._preset_combo.set(safe)
        self._log(f"[Presets] Saved → {safe}")
```

- [ ] **Step 4: Add `_new_preset`**

```python
    def _new_preset(self) -> None:
        dialog = ctk.CTkInputDialog(text="Name for new preset:", title="New Preset")
        name = dialog.get_input()
        if not name:
            self._preset_combo.configure(values=self._preset_values())
            self._preset_combo.set("")
            return
        safe = re.sub(r'[^\w\s\-]', '', name.strip()).strip()
        if not safe:
            self._log("[Presets] Invalid name — use letters, numbers, spaces, or dashes.")
            self._preset_combo.configure(values=self._preset_values())
            self._preset_combo.set("")
            return
        path = os.path.join(self._presets_dir, f"{safe}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.command_map, f, indent=2)
        self._preset_combo.configure(values=self._preset_values())
        self._preset_combo.set(safe)
        self._log(f"[Presets] Created preset '{safe}' with current mappings.")
```

- [ ] **Step 5: Verify app still starts cleanly (no syntax errors)**

```bash
DISPLAY=:0 .venv/bin/python twitch_bot.py
```

Expected: app opens, no tracebacks.

- [ ] **Step 6: Commit**

```bash
git add twitch_bot.py
git commit -m "feat: add preset helper methods (_list_presets, _save_preset, _new_preset, _on_preset_selected)"
```

---

### Task 3: Add preset bar UI to `_build_plays`

**Files:**
- Modify: `twitch_bot.py:820-877` — `_build_plays` method

- [ ] **Step 1: Shift existing row indices**

In `_build_plays`, the current layout is:
- row 0 → toggle header (`hdr`)
- row 1 → add-mapping frame (`add`)
- row 2 → scrollable list (`self._plays_scroll`)

Change row index of the `add` frame from `row=1` to `row=2`:
```python
        add.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 6))
```

Change row index of the scroll frame from `row=2` to `row=3`, and update `grid_rowconfigure` from `2` to `3`:
```python
        tab.grid_rowconfigure(3, weight=1)
        ...
        self._plays_scroll.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 10))
```

- [ ] **Step 2: Insert the preset bar at row=1**

After the toggle header block (after the `self.game_input_enabled.trace_add(...)` call, before the `# Add-mapping controls` comment), insert:

```python
        # Preset bar
        preset_bar = ctk.CTkFrame(tab, corner_radius=8)
        preset_bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))

        ctk.CTkLabel(preset_bar, text="Preset:").pack(side="left", padx=(14, 4), pady=10)
        self._preset_combo = ctk.CTkComboBox(
            preset_bar,
            values=self._preset_values(),
            command=self._on_preset_selected,
            width=180,
        )
        self._preset_combo.set("")
        self._preset_combo.pack(side="left", padx=4)

        ctk.CTkButton(
            preset_bar, text="Save", width=80,
            command=self._save_preset,
        ).pack(side="left", padx=10)
```

- [ ] **Step 3: Start the app and visually verify the preset bar**

```bash
DISPLAY=:0 .venv/bin/python twitch_bot.py
```

Expected:
- The Twitch Plays panel now shows, top to bottom: toggle header → preset bar (label + dropdown + Save button) → add-mapping controls → scrollable list
- Dropdown shows "+ New Preset" (no other entries yet)
- No layout breakage or tracebacks

- [ ] **Step 4: Commit**

```bash
git add twitch_bot.py
git commit -m "feat: add preset bar UI to Twitch Plays panel"
```

---

### Task 4: End-to-end manual test

- [ ] **Step 1: Test creating a new preset**

1. Add a mapping: command `!jump`, key `space`, hold `0.5` → click "+ Add Mapping"
2. Add a mapping: command `!left`, key `a`, hold `0.3` → click "+ Add Mapping"
3. Click the preset dropdown → select "+ New Preset"
4. Enter name `Mario` in the dialog → click OK

Expected:
- Console shows `[Presets] Created preset 'Mario' with current mappings.`
- Dropdown now shows `Mario` as selected
- File `plays_presets/Mario.json` exists and contains both mappings

- [ ] **Step 2: Test saving changes to an existing preset**

1. Add another mapping: `!right`, key `d`, hold `0.3`
2. Click **Save**

Expected:
- Console shows `[Presets] Saved → Mario`
- `plays_presets/Mario.json` now contains all three mappings

- [ ] **Step 3: Test loading a preset (replace)**

1. Click "+ Add Mapping" to add a fourth entry: `!extra`, key `x`, hold `0.1` (now 4 mappings visible)
2. From the dropdown, select `Mario`

Expected:
- Console shows `[Presets] Loaded: Mario`
- Command list shows exactly the 3 mappings saved in step 2 — the `!extra` entry is gone

- [ ] **Step 4: Test creating a second preset**

1. Clear all mappings manually (Remove each one)
2. Add: `!fire`, key `z`, hold `0.2`
3. New Preset → name `FPS`

Expected:
- Dropdown now lists both `FPS` and `Mario`
- Switching between them loads the correct mapping sets

- [ ] **Step 5: Test persistence across restarts**

1. Close the app
2. Reopen: `DISPLAY=:0 .venv/bin/python twitch_bot.py`

Expected:
- Preset dropdown lists `FPS` and `Mario` (files persist)
- The `plays_presets/` directory still contains both `.json` files

- [ ] **Step 6: Commit and push**

```bash
git add twitch_bot.py
git commit -m "test: verify plays presets end-to-end manually"
git push
```
