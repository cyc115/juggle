# juggle — Overview

> **Navigation aid.** This article shows WHERE things live (routes, models, files). Read actual source files before implementing new features or making changes.

**juggle** is a javascript project built with raw-http.

## Scale

11 library files · 8 environment variables

**Libraries:** 11 files — see [libraries.md](./libraries.md)

## Required Environment Variables

- `_JUGGLE_TEST_DB` — `src/juggle_cli_common.py`
- `CLAUDE_PLUGIN_DATA` — `src/juggle_hooks.py`
- `JUGGLE_IDLE_THRESHOLD_SECS` — `src/juggle_cli_common.py`
- `JUGGLE_MAX_BACKGROUND_AGENTS` — `src/juggle_db.py`
- `JUGGLE_MAX_THREADS` — `src/juggle_db.py`
- `JUGGLE_TMUX_MOCK_KILL` — `src/juggle_tmux.py`
- `JUGGLE_TMUX_MOCK_PANE` — `src/juggle_tmux.py`
- `JUGGLE_TMUX_MOCK_SEND` — `src/juggle_tmux.py`

---
_Back to [index.md](./index.md) · Generated 2026-04-15_