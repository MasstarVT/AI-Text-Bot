# Plays Presets — Design Spec
**Date:** 2026-07-02

## Overview

Add save/load/new preset functionality to the Twitch Input Plays panel. Presets store a named snapshot of the current command map (command → key + hold duration) as individual JSON files in a `plays_presets/` directory.

## Storage

- **Directory:** `plays_presets/` next to `twitch_bot.py`, created on first run (`os.makedirs(..., exist_ok=True)`)
- **File format:** `<name>.json` — a single JSON object matching the `command_map` dict shape:
  ```json
  {
    "!jump":  {"key": "space",  "duration": 0.5},
    "!left":  {"key": "a",     "duration": 0.3}
  }
  ```
- **Name sanitization:** same regex as prompt names — `re.sub(r'[^\w\s\-]', '', name).strip()`
- **No changes to `settings.json`** — `command_map` continues to store live state between sessions as before; presets are a separate external concept

## UI Changes

A **preset bar** is inserted into `_build_plays()` as a new row between the toggle header (row 0) and the add-mapping controls. Existing row indices shift down by one (add-controls → row 2, scroll frame → row 3).

The preset bar contains (left to right):
- `CTkLabel` — "Preset:"
- `CTkComboBox` (`self._preset_combo`) — values: `["+ New Preset"] + sorted preset names`
- `CTkButton` — "Save" — calls `_save_preset()`

**Load on select:** selecting an existing name from the dropdown immediately loads and replaces the active command map, then refreshes the plays list. No separate Load button needed.

**New Preset flow:** selecting "+ New Preset" opens a `CTkInputDialog` asking for a name, then saves the current command map into that new file and updates the dropdown.

## New Methods

| Method | Behaviour |
|---|---|
| `_list_presets()` | Scans `plays_presets/` for `.json` files; returns sorted name list (strip `.json`) |
| `_preset_values()` | Returns `["+ New Preset"] + _list_presets()` |
| `_on_preset_selected(name)` | If "+ New Preset" → `_new_preset()`; else load JSON file, replace `self.command_map`, call `_refresh_plays()`, log |
| `_save_preset()` | Read current name from combo; if blank or "+ New Preset" → log error; else write `self.command_map` as JSON to file, refresh combo, log |
| `_new_preset()` | Open `CTkInputDialog` for name; sanitize; save current `command_map` to new file; update combo to show new name; log |

## Initialisation

- `self._presets_dir` set in `__init__` alongside `self._prompts_dir`
- `os.makedirs(self._presets_dir, exist_ok=True)` called in `__init__`

## Error Cases

- Empty or invalid name → log error, do nothing
- Preset file missing on load (e.g. deleted externally) → log error, do nothing
- Malformed JSON on load → catch `Exception`, log error, do nothing
