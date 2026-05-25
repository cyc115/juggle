# Changelog

## 2026-05-24 (v1.31.1)
- talkback: event-driven device selection via CoreAudio `AudioObjectAddPropertyListener` — callback sets `_devices_dirty=True` on any device-list change; `_play_audio` reinitialises PortAudio (`sd._terminate/initialize`) + re-picks + caches the chain only when dirty, zero overhead on clean calls; falls back to per-call detection if reinit fails so audio never breaks; logs old→new device name on change; `pyobjc-framework-CoreAudio` added to inline deps, guarded with try/except for non-macOS
- tests: 4 new TDD tests in `tests/test_talkback_device_cache.py` covering cached-chain reuse, dirty-flag reinit+repick+clear, cold-start cache build, and listener-unavailable fallback

## 2026-05-24 (v1.31.0)
- cockpit: add `--profile [--duration N]` harness — spawns a headless worker child that runs the 1-second snapshot+render loop for N seconds (default 60), profiles it with `psrecord` via `uvx`, then prints a summary: avg/peak CPU%, RSS start/end/growth/peak; flags RSS growth > 20 MB (possible leak) and avg CPU > 15% (battery concern); degrades gracefully if `uvx`/`psrecord` is unavailable (exits 0 with a clear message); available as `cockpit --profile` and `juggle_cockpit.py --profile`
- tests: 6 new TDD tests in `tests/test_cockpit_profile.py` covering `_parse_psrecord_log` (basic parse, empty log, threshold detection) and `_profile_worker_loop` (N-iteration count and zero-duration via mocked clock)

## 2026-05-24 (v1.30.2)
- cockpit: removed legacy v1 (Rich) cockpit; the Textual cockpit is now the only one; dropped the `--v2` flag from `juggle_cli.py cockpit`
- cockpit: add `--out` static render mode — prints all four panes as plain text to stdout then exits (no TUI); available as `juggle_cli.py cockpit --out` and `juggle_cockpit.py --out`; backed by `render_static_from_state` / `render_static` in juggle_cockpit_view.py
- fix(tests): restore sys.modules["numpy"] after _load_talkback() import — MagicMock was leaking into pytest.approx, breaking persist_ratios tests in full-suite runs
- cockpit: restore Notifications to full-width bottom of the right region (was incorrectly a 3rd column in #upper); `--out` now mirrors the 2D layout (Topics left, Actions+Agents top-right, Notifications full-width bottom-right); add `HSplitter` for vertical drag-to-resize between the Actions/Agents row and Notifications

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
