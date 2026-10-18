# logi-omm-mac

A small macOS replacement for Logitech's Windows-only Onboard Memory Manager, built for the
**G604 LIGHTSPEED**. It edits the button bindings stored on the mouse itself, so they work
on any computer without G HUB or any other driver running.

Requires [uv](https://docs.astral.sh/uv/). Your terminal needs **Input Monitoring** permission
(System Settings > Privacy & Security > Input Monitoring).

## Button layout

Each G-button maps to a slot in the mouse's profile (slot = G-number - 1).

| Button | Slot | Factory default | After `remap-g4-g9.sh` |
|--------|------|-----------------|------------------------|
| G4     | 3    | DPI shift       | mouse button 9         |
| G5     | 4    | mouse button 4 (back)    | mouse button 4 (back)    |
| G6     | 5    | mouse button 5 (forward) | mouse button 5 (forward) |
| G7     | 6    | keyboard `3`    | mouse button 8         |
| G8     | 7    | keyboard `2`    | mouse button 7         |
| G9     | 8    | keyboard `1`    | mouse button 6         |

Generic mouse buttons 6–9 don't do anything by default, so apps like BetterMouse, BetterTouchTool,
or a game's keybind menu can bind them to whatever you want.

Run `uv run omm.py info` to see the current bindings for every profile.

## Remap: `remap-g4-g9.sh`

```sh
./remap-g4-g9.sh                # apply to the active profile
./remap-g4-g9.sh --profile 2    # apply to a specific profile (1-5)
./remap-g4-g9.sh --dry-run      # show what would change, write nothing
```

Binds G4–G9 to the mouse buttons in the table above. It's safe to run anytime:

- If every button already matches, nothing is written.
- Otherwise it saves a backup of the profile sectors to `backups/`, writes all six bindings at
  once, reads the sector back to verify it, and reloads the profile.
- Only one profile is changed per run (the active one unless you pass `--profile`).

The mapping lives in `MOUSE_BUTTON_FOR_G_BUTTON` in `omm.py`.

## Revert: `revert-g4-g9.sh`

```sh
./revert-g4-g9.sh --dry-run     # preview
./revert-g4-g9.sh               # restore profile 1 to its original bindings
```

Writes `backups/20261016-113914-profile1-user-sector-0x0001.bin` (taken before any remapping)
back to profile 1. **Don't delete that file.** The revert covers profile 1 only. To undo a remap
on another profile, restore that profile's backup from before the remap:

```sh
uv run omm.py restore backups/<timestamp>-profile2-user-sector-0x0002.bin
```

## All commands

```sh
uv run omm.py info                                   # show profiles and bindings
uv run omm.py dump                                   # back up all sectors to backups/
uv run omm.py set-button SLOT BUTTON [--profile N]   # bind one slot to a mouse button
uv run omm.py remap-g [--profile N]                  # what remap-g4-g9.sh runs
uv run omm.py restore BACKUP_FILE                    # write a backup back to its sector
```

Every writing command accepts `--dry-run`.
