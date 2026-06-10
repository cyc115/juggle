# Refactor Results — Understanding & Token Efficiency (2026-06-10)

Companion results report for `plan/2026-06-10-refactor-for-understanding-and-tokens.md`.
Before column is the canonical baseline (main @ 88dc30e, gate baseline measured on
branch cyc_refactor-tokens after the hermetic test fix bd4b099).

## Before / After

| Metric | Before (2026-06-10) | After |
|---|---|---|
| Total Python LOC (git-tracked `*.py`) | 43,664 across 143 files | |
| `src/` modules >300 lines | 22 | |
| All gated files >300 lines (src `*.py` + python scripts) | 23 (incl. `scripts/talkback` 415) | |
| loc_gate allowlist entries | 23 | |
| Tests collected | 1390 | |
| Full-suite result | 1372 passed, 18 skipped (161.82s) | |
| `JuggleDB` god-node edges (graphify) | 1392 | |
| Graph size | 6667 nodes · 11633 edges (commit b378fac) | |

Top-10 src LOC (before):

```
1962 src/juggle_db.py          1098 src/juggle_cmd_agents.py
1332 src/juggle_watchdog.py    1056 src/juggle_hooks.py
1120 src/juggle_cockpit.py     1006 src/juggle_cli.py
 839 src/juggle_tmux.py         823 src/juggle_schedule_autofix.py
 737 src/juggle_cmd_projects.py 673 src/juggle_cmd_threads.py
```

## Baseline notes

- Baseline on unmodified main showed 7 pre-existing failures under the documented
  test env (`JUGGLE_MAX_THREADS=10`): `tests/test_juggle_settings.py` (env-override
  precedence) and `tests/test_juggle_smoke.py` (`_make_db` seeds 30 threads past the
  cap). Fixed test-side in bd4b099 before any refactor work; suite fully green since.

## Phase progress

- **Phase 0** done (57b2ca6): loc gate + tests + this baseline doc. Gate green,
  23 grandfathered entries.
- **Phase 1** done:
  - 1.1 (679e2de): `src/daemon_pidfile.py` single source of truth; monitor /
    watchdog-script / juggle_watchdog shims preserve per-site semantics.
    Finding: the monitor's old docstring claimed process-group kill, but its
    code always did single-pid SIGTERM→wait→SIGKILL — both kill paths were
    already identical; only logging and pidfile-write verification differ.
  - 1.2 (499fe89): `src/llm_calls.py` (`run_claude_p` + moved `llm_call`),
    four call sites rewired behavior-preserving. Allowlist shrunk: 23 → 22
    entries (`juggle_schedule_common.py` now 297 lines; watchdog budget
    1332→1301, cmd_projects 737→735).
  - 1.3 (aad7ea0): schedule-common tests unified in
    `tests/schedule/test_schedule_common.py` — 95 collected (exact union,
    zero exact duplicates found, nothing dropped); flat file deleted.
    Total collection 1400 (baseline 1390 + 10 loc-gate tests).
- Pre-phase: gate-language docs fix (ebcc898) and hermetic-env test fix
  (bd4b099) — see Baseline notes.

## Reverts / abandonments

(none yet)

## Follow-ups (deferred behavior changes)

- Unify pidfile kill semantics (monitor: group-kill+wait vs watchdog: single
  SIGTERM) — Phase 1 preserves each call-site's behavior via flags; unification is
  a deliberate behavior change deferred out of this refactor.
