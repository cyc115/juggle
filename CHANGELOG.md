# Changelog

## 2026-05-25 (v1.33.0)
- cockpit: tail is now a **modal overlay** (`_TailModal`) — `t` pushes a centered ~80%×70% bordered overlay over the UI with a 1s `set_interval` live refresh (injected `capture_fn`), replacing the inline `#tail` Static drawer; drawer state (`_tail_active`/`_tail_pane_id`), the `#tail` widget, and the `_refresh` drawer block are removed from `juggle_cockpit.py`
- cockpit: `_tmux_capture_pane` reads scrollback via `capture-pane -S -<lines>` so tail returns the last N lines regardless of pane display height (was visible-region-only)
- cockpit refactor: `juggle_cockpit.py` (1396L) split into `juggle_cockpit_helpers.py` (pure helpers), `juggle_cockpit_modals.py` (modal screens), `juggle_cockpit_widgets.py` (`Splitter`/`HSplitter`); re-exports preserve all existing imports (main file → ~1008L)
- watchdog: `alive_slow` (alive-but-slow) agents now surface as a passive notification via `add_notification_v2` instead of a `failure` action item — the Enter nudge is unchanged
- watchdog/start: `/juggle:start` is **idempotent per Claude session** — pidfile is session-scoped (`watchdog-<CLAUDE_CODE_SESSION_ID>.pid`, falls back to `watchdog.pid` when unset) and `_start_watchdog` kill-then-restarts only this session's watchdog (SIGTERM → 2s poll → SIGKILL → unlink → respawn); other sessions' watchdogs are never touched; talkback (shared singleton) untouched
- tests: full suite 858 passed / 5 skipped

## 2026-05-25 (v1.32.3)
- fix(tmux): `wait_for_submission` now captures scrollback tail via `capture-pane -S -10` instead of visible-only `capture-pane -pt`; submission markers and stuck-state (`[Pasted text`, head, ❯/> prompt) are evaluated against the last `_DETECT_TAIL_LINES=10` lines of the returned output, making detection pane-size-independent; `_BOTTOM_REGION_LINES` constant removed (superseded by `_DETECT_TAIL_LINES`)
- tests: 3 new TDD tests — `test_wait_for_submission_capture_uses_scrollback_flag` (asserts -S present in capture-pane args), `test_wait_for_submission_detects_marker_in_scrollback_tail` (marker in last 10 lines of 50-line buffer), `test_wait_for_submission_detects_stuck_in_scrollback_tail` (stuck placeholder in tail triggers C-m retry)

## 2026-05-25 (v1.32.2)
- fix(tmux): `wait_for_submission` now requires a `_SUBMISSION_MARKERS` token ("esc to interrupt" / "✻" / "✶") for success — removed the `head not in bottom → True` false-positive branch that caused tasks to sit unsubmitted when Claude Code collapses large pastes into a `[Pasted text #N +M lines]` placeholder; stuck detection covers collapsed-paste placeholder, head-in-bottom (short prompts), and non-empty ❯/> prompt lines; C-m retry fires immediately on every stuck poll (no consecutive-stuck delay); `max_enter_retries` raised 3→5; settle delay before first C-m bumped 0.15s→0.4s
- tests: 2 new RED→GREEN regression tests (`test_wait_for_submission_collapsed_paste_does_not_false_positive`, `test_wait_for_submission_collapsed_paste_retries_enter_then_succeeds`); 3 existing tests updated to new marker-only success contract

## 2026-05-25 (v1.32.1)
- watchdog: cold-boot grace period — `execute_recovery` skips decommission for never-tasked agents younger than `agent_boot_grace_secs` (default 120s); uses `created_at` (fallback `last_active`) for age; old stale-boot agents (age ≥ grace) still decommissioned; `_BOOT_GRACE_SECS=120` module constant; `agent_boot_grace_secs` added to `juggle_settings` DEFAULTS; `_get_agent_age_secs` pure helper
- tests: 2 new TDD tests (`test_young_never_tasked_agent_not_decommissioned`, `test_old_never_tasked_agent_still_decommissioned`); updated 9 existing tests across 4 files to backdate `created_at` so old-agent paths still exercise decommission

## 2026-05-25 (v1.32.0)
- cockpit: keyboard shortcuts — `s` switch thread by label (PromptModal → set_current_thread), `a` ack all open actions on a thread by label (PromptModal → dismiss_action_items_for_thread), `?` help overlay (deduplicates aliased scroll-key rows), `j`/`k`/`↑`/`↓`/`PgUp`/`PgDn` scroll active pane via named BINDINGS (replaces on_key handler), `Tab` cycles pane; no manual-refresh key (`r` removed — 1s auto-tick is sufficient); pure helpers `_resolve_thread_by_label` / `_resolve_actions_by_thread_label` module-level for testability
- tests: 14 new TDD tests in `tests/test_cockpit_keys.py` (pure-helper unit tests + Textual Pilot integration tests for switch/ack/not-found paths)

## 2026-05-24 (v1.31.2)
- watchdog: fix false high-priority alert for "spawned but never tasked" agents — `execute_recovery` now detects `last_task=None/""` with an early-return path that silently decommissions (kill pane, delete agent, `decommissioned_untasked` watchdog event) without writing a snapshot, filing an action item, or marking the thread failed; `scripts/juggle-agent-watchdog` now passes `last_send_task_at=agent.get("last_send_task_at")` to `classify_pane_state` so agents waiting for their first dispatch are classified as `awaiting_dispatch` (not `stalled`) and recovery is skipped
- tests: 6 new TDD tests in `tests/test_watchdog_never_tasked.py`; updated 10 existing tests across 4 files that asserted the old buggy behaviour

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
