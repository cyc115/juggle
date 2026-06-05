# Cockpit Viewport Smoke Harness

**Date:** 2026-06-04  
**Version:** 1.44.0

## Problem

The juggle cockpit TUI is developed and tested at one viewport size (typically the developer's terminal). Layout regressions (overflow, blank panels, missing chrome) at other common sizes go undetected until reported by users.

## Solution

A pty+pyte smoke harness that renders the cockpit at each of 7 standard viewport profiles, drives real keyboard input, and applies layout heuristics to detect regressions automatically.

## Architecture

### Viewport Profiles (`config/viewports.yaml`)

| Profile   | Cols | Rows | Description                      |
|-----------|------|------|----------------------------------|
| 2k_full   | 240  | 67   | 2K monitor, fullscreen           |
| 2k_half   | 120  | 67   | 2K monitor, half-screen split    |
| 2k_third  | 80   | 67   | 2K monitor, one-third column     |
| portrait  | 110  | 130  | Monitor rotated 90°, vertical    |
| custom_1  | 100  | 50   | User-defined slot 1              |
| custom_2  | 160  | 48   | User-defined slot 2              |
| custom_3  | 200  | 55   | User-defined slot 3              |

### PTY Driver (`src/juggle_smoke.py`)

`open_cockpit_pty(profile, db_path)` spawns `juggle_cockpit.py` in a real pseudo-terminal:
- `pty.openpty()` + `TIOCSWINSZ` sets exact viewport dimensions before child starts
- `TERM=xterm-256color`, `COLUMNS`/`LINES` env vars for Textual
- `pyte.Screen` + `pyte.Stream` processes ANSI output into a text grid

`CockpitHandle` API:
- `.send(key: bytes)` — write key bytes to master fd (real terminal input)
- `.frame(settle, timeout)` — poll until stable + footer chrome visible, return `list[str]`
- `.resize(cols, rows)` — `TIOCSWINSZ` + `SIGWINCH` mid-session
- `.close()` — send `q`, wait for clean exit

**Stability detection:** `frame()` requires both output stability (no new bytes for 150ms) AND the footer keybinding bar to be visible in the last row. This prevents locking onto a partial first paint where only header+border appear.

### Layout Heuristics

| Check           | Pass Condition                               | Hard fail? |
|-----------------|----------------------------------------------|------------|
| `check_overflow`     | No rendered line wider than `cols`      | Yes        |
| `check_real_estate`  | ≤40% of rows are entirely blank         | Yes        |
| `check_chrome_present` | Header (Juggle/Cockpit) in top 3 rows, keybinding bar in bottom 3 | Yes |
| `check_truncation`   | Count of `…` markers (informational)    | No (warn)  |

### CLI

```bash
# Single viewport
uv run src/juggle_cli.py cockpit --smoke --viewport 2k_third

# All viewports
uv run src/juggle_cli.py cockpit --smoke --all-viewports

# Interactive mode (nav + resize + tab cycle)
uv run src/juggle_cli.py cockpit --smoke --all-viewports --interactive

# JSON output for CI
uv run src/juggle_cli.py cockpit --smoke --all-viewports --json
```

### Frame Dumps

Rendered frames are written to `data/cockpit-viewport-review/<profile>.txt` (gitignored). Use these for manual visual review after a run.

## Testing (`tests/test_juggle_smoke.py`)

- 12 pure tests (no external deps): viewport loader (4), heuristic functions (8)
- 4 PTY integration tests (skipped on Windows or `SMOKE_SKIP=1`):
  - `test_render_2k_third_no_overflow_and_frame_file_written`
  - `test_nav_j_key_produces_visible_change`
  - `test_resize_reflows_no_overflow`
  - `test_flow_tab_cycles_pane_grid_changes`

## Standing Rule

Run `cockpit --smoke --all-viewports` after any cockpit layout change before merging. All 7 profiles must pass.
