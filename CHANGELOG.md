# Changelog

## 2026-05-24
- cockpit: removed legacy v1 (Rich) cockpit; the Textual cockpit is now the only one; dropped the `--v2` flag from `juggle_cli.py cockpit`
- cockpit: add `--out` static render mode — prints all four panes as plain text to stdout then exits (no TUI); available as `juggle_cli.py cockpit --out` and `juggle_cockpit.py --out`; backed by `render_static_from_state` / `render_static` in juggle_cockpit_view.py
- cockpit: notifications pane is now a horizontally drag-resizable third column in `#upper` (alongside Actions and Agents) rather than a fixed-height bottom strip

## 2026-05-21
- talkback: log every /speak request to ~/.juggle/logs/talkback.jsonl (text, voice, speed, ts, client_ip, cancelled flag) for future analysis. Override path via JUGGLE_TALKBACK_LOG_PATH env var.
- docs(readme): concise rewrite — cut filler, sharpen tagline, refresh examples to uv run / cockpit --v2
- docs: refresh README hero screenshot with Cockpit v2 + orchestrator + parallel-coder example; bump version badge to 1.28.2
- fix(cockpit v2): palette close no longer resets dragged column widths — `on_resize` "wide" branch now only resets on narrow→wide transition, not on every resize; also fixes missing `#actions`/`#agents` reset during narrow→wide restore

## 2026-05-20
- juggle coder dispatch: default to TDD (test-driven-development invoked before executing-plans in both /juggle:start and /juggle:delegate templates)
- cockpit v2: persist current column widths to `~/.juggle/config.json` on quit (exit() override hook + atomic tmp→rename write)
- Add `juggle_cockpit_v2.py` — Textual cockpit with mouse drag-to-resize between panels; opt-in via `juggle_cli.py cockpit --v2`; v1 (Rich) unchanged and default
- Add `cockpit` subcommand to `juggle_cli.py` (`--v2` flag launches v2; without flag launches v1)
- Add `/juggle:search-offline-db` — lightweight KB-only search (no synthesis, no vault/memory/web). Supports `--fts` for fully-offline mode.
- Fix talkback Bluetooth-device routing: pick output by index, not name, so HFP/A2DP duplicate names don't shadow the A2DP entry

## 2026-05-19
- Add `/schedule:dogfood`, `:autofix`, `:reflect` skills for background automation routines
- Add autofix fix-types: fx3 (test-gap analysis), fx4 (watchdog test improvements), fx5 (doc drift), fx6 (CHANGELOG append)
- Fix watchdog handling of undispatched agents, recovery crashes, and stale task state
- Tighten `.gitignore` for runtime artifacts; add Juggle Cockpit screenshot to docs

## 2026-05-18
- **Watchdog daemon**: agent monitoring with stuck-at-prompt detection, orphaned thread recovery, and automatic retry logic
- **Schedule skills**: new `/schedule:dogfood`, `:autofix`, `:reflect` for automated agent workflows and house-keeping routines
- **Action items auto-filing**: agents now auto-create action items on release/planner/draft completions; improved A2 keyword matching with phrase patterns
- **Research/KB**: knowledge base with HN/PDF ingestion + semantic search; multi-query research agent with behavioral guardrails
- **Cockpit**: scheduled tasks panel; agent display shows role+topic+age instead of agent IDs
- **Doctor**: new `/juggle:doctor` diagnostic command for config + DB migration checks
- Refactor: drop domain machinery (no longer used)
