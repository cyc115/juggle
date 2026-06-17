# Selfheal Auto-Diagnosis Loop

Date: 2026-06-17

## Problem

`src/juggle_selfheal.py` is write-only: `record_error()` captures errors but
`_try_claim_diagnosis_slot()` is dead code. Rows sit `status='open'` forever.

## Design (RESOLVED — do not redesign)

- **Status lifecycle**: open → diagnosing → awaiting_approval → resolved
- **No schema migration**: `awaiting_approval` already in enum; no `diagnosed_at` column
- **Retention**: auto-purge rows older than `selfheal.retention_days` (default 14) days
  triggered on session-start hook and watchdog tick. No manual bulk-resolve command.
- **Reentrancy**: `JUGGLE_SELFHEAL_OP` env var already guards `record_error`; verify it
  is set on the dispatch path.
- **Dispatch mechanism**: reuse `_dispatch_via_pool` from `juggle_graph_dispatch` + 
  `db.create_thread()`, same pattern as `graph_tick`.

## Task List

1. **Config** — add `selfheal` key to `juggle_settings.py` DEFAULTS:
   `enabled=False`, `min_count=3`, `retention_days=14`
2. **`get_diagnosis_candidates(db)`** — DB query: status='open' AND error_class='A'
   AND count >= min_count, ordered by count DESC
3. **`select_diagnosis_candidate(rows, in_flight_exists)`** — pure gate, no DB:
   returns row or None
4. **`reset_stale_diagnosing_rows(db, now)`** — rows stuck diagnosing > 3× poll
   interval (90s × 3 = 270s) reset to 'open'
5. **`purge_expired_selfheal(db, now, retention_days)`** — DELETE where
   last_seen < (now - retention_days days)
6. **`build_diagnosis_prompt(row)`** — pure prompt builder (no DB)
7. **`maybe_dispatch_selfheal_diagnosis(db)`** — top-level orchestration:
   candidates → select → claim → create_thread → dispatch → set awaiting_approval
8. **Reentrancy guard** — verify `JUGGLE_SELFHEAL_OP` is set before dispatch;
   confirm `record_error` honors it (already does)
9. **`list-selfheal --json`** — add `--json` flag to `_cmd_list_selfheal`
10. **Harness gate** — `tests/test_selfheal_diagnosis.py` covering end-to-end gate
    without real agent dispatch
11. **Wire into watchdog `_poll_once`** — call `maybe_dispatch_selfheal_diagnosis`
    fire-and-forget after `graph_tick`
12. **Wire retention purge into `handle_session_start`** — call `purge_expired_selfheal`
13. **Version bump** — minor bump in `.claude-plugin/plugin.json`

## Files to Change

- `src/juggle_settings.py` — add selfheal config defaults
- `src/juggle_selfheal.py` — add all new functions (tasks 2-8)
- `src/juggle_cmd_misc.py` — add --json to list-selfheal
- `src/juggle_watchdog_daemon.py` — wire maybe_dispatch_selfheal_diagnosis
- `src/juggle_hooks_checkpoint.py` — wire purge_expired_selfheal
- `tests/test_selfheal_diagnosis.py` — new test file
- `.claude-plugin/plugin.json` — version bump
