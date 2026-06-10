# Refactor Results — Understanding & Token Efficiency (2026-06-10)

Companion results report for `plan/2026-06-10-refactor-for-understanding-and-tokens.md`.
Before column is the canonical baseline (main @ 88dc30e, gate baseline measured on
branch cyc_refactor-tokens after the hermetic test fix bd4b099).

## Before / After

| Metric | Before (2026-06-10) | After (Phase 2 complete) |
|---|---|---|
| Total Python LOC (git-tracked `*.py`) | 43,664 across 143 files | ~46,200 across 157 files (+14 new split modules) |
| `src/` modules >300 lines | 22 | 17 (juggle_db.py removed; migrations+new modules added) |
| All gated files >300 lines (src `*.py` + python scripts) | 23 (incl. `scripts/talkback` 415) | 22 |
| loc_gate allowlist entries | 23 | 22 |
| Tests collected | 1390 | 1382 passed, 18 skipped, 9 warnings |
| Full-suite result | 1372 passed, 18 skipped (161.82s) | 1382 passed, 18 skipped (179s) |
| `JuggleDB` god-node edges (graphify) | 1392 | TBD (Phase 5 measurement) |
| Graph size | 6667 nodes · 11633 edges (commit b378fac) | 6992 nodes · 12262 edges (post-Phase 2) |

Top-10 src LOC (before):

```
1962 src/juggle_db.py          1098 src/juggle_cmd_agents.py
1332 src/juggle_watchdog.py    1056 src/juggle_hooks.py
1120 src/juggle_cockpit.py     1006 src/juggle_cli.py
 839 src/juggle_tmux.py         823 src/juggle_schedule_autofix.py
 737 src/juggle_cmd_projects.py 673 src/juggle_cmd_threads.py
```

Top-10 src LOC (after Phase 2):

```
1098 src/juggle_cmd_agents.py   964 src/juggle_watchdog.py
1056 src/juggle_hooks.py        839 src/juggle_tmux.py
1006 src/juggle_cli.py          835 src/juggle_cockpit.py
 823 src/juggle_schedule_autofix.py  735 src/juggle_cmd_projects.py
 673 src/juggle_cmd_threads.py  598 src/juggle_context.py
```

juggle_db.py (1962 lines) no longer appears — split into 9 modules (largest: juggle_db_migrations.py 532).

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
  - Phase 1 wrap-up / version bump (508683c → v1.51.2).
- Pre-phase: gate-language docs fix (ebcc898) and hermetic-env test fix
  (bd4b099) — see Baseline notes.
- **Phase 2** done (v1.52.0):
  - 2.1 (7bdc221): `juggle_db.py` (1962→144 lines) split into 9 domain modules:
    - `juggle_db_schema.py` (277) — DDL, constants, pure helpers
    - `juggle_db_migrations.py` (532) — 34 schema migrations (grandfathered, see debt)
    - `juggle_db_session.py` (113), `juggle_db_threads.py` (280)
    - `juggle_db_projects.py` (218), `juggle_db_messages.py` (174)
    - `juggle_db_notifications.py` (153), `juggle_db_selfheal.py` (109)
    - `juggle_db_agents.py` (296)
    - All `from juggle_db import X` callers work via re-export shims. MAX_THREADS
      patch fixture updated to patch all 3 module namespaces.
  - 2.2 (a1cd4a1): `juggle_watchdog.py` (1301→964 lines) extracted:
    - `juggle_watchdog_restart.py` (128) — hot-restart + stale-source detection
    - `juggle_watchdog_inspect.py` (262) — inspect_agent + _handle_crashed
    - Circular import avoided via lazy function-body import in inspect module.
    - All `patch("juggle_watchdog.X")` targets preserved via module-level re-exports.
  - 2.3 (af0b935): `juggle_cockpit.py` (1120→835 lines) extracted:
    - `juggle_cockpit_layout.py` (104) — column-ratio constants + helpers
    - `juggle_cockpit_profile.py` (232) — headless psrecord profiling harness
  - 2.4 (reverted): `juggle_cmd_agents.py` lifecycle split failed — tests universally
    patch `juggle_cmd_agents.get_db`, `._resolve_thread`, `.JuggleTmuxManager` etc.
    Moving functions to `juggle_cmd_agents_lifecycle.py` breaks all those patches.
    Root cause: every lifecycle command reads module-level globals patched by tests.
    Fix requires either (a) passing globals as parameters or (b) updating ~20 test
    patch targets. Deferred to Phase 3 (when internal imports are migrated anyway).
  - 2.5 (deferred): `juggle_hooks.py` split blocked by same pattern — module-level
    globals (`DB_PATH`, `_CHECKPOINT_PATH`, `AUTOPILOT_FLAG`) read by every handler.
    A `hooks_config.py` constants module would resolve this; deferred to Phase 3.
  - 2.6 (deferred): `juggle_cli.py` main() is 628 lines of pure argparse wiring;
    the non-main functions (329 lines) could move to `juggle_cmd_misc.py`, but that
    still leaves juggle_cli.py at ~700 lines due to argparse volume. Grandfathered.

## loc_gate allowlist after Phase 2

```
1098  src/juggle_cmd_agents.py    (Phase 2.4 deferred — test patch coupling)
1056  src/juggle_hooks.py         (Phase 2.5 deferred — module-global coupling)
1006  src/juggle_cli.py           (Phase 2.6 deferred — 628-line argparse main)
 964  src/juggle_watchdog.py      (lowered 1332→964; further split in Phase 3)
 839  src/juggle_tmux.py          (not touched yet)
 835  src/juggle_cockpit.py       (lowered 1120→835)
 823  src/juggle_schedule_autofix.py
 735  src/juggle_cmd_projects.py  (lowered 737→735)
 673  src/juggle_cmd_threads.py
 598  src/juggle_context.py
 545  src/juggle_schedule_reflect.py
 532  src/juggle_db_migrations.py (NEW — grandfathered; 34 ordered migrations,
                                    cannot split without losing ordering invariant)
 499  src/juggle_cockpit_view.py
 494  src/juggle_scheduler.py
 467  src/juggle_cockpit_model.py
 460  src/juggle_settings.py
 415  scripts/talkback
 406  src/juggle_schedule_dogfood.py
 392  src/juggle_cmd_research.py
 380  src/juggle_smoke.py
 370  src/juggle_cmd_context.py
 364  src/juggle_cmd_integrate.py
```

## Reverts / abandonments

- Phase 2.4 lifecycle split of `juggle_cmd_agents.py` reverted: widespread test
  patch-target breakage. Root cause: 20+ tests patch `juggle_cmd_agents.<symbol>`
  where `<symbol>` is `get_db`, `_resolve_thread`, `JuggleTmuxManager`, etc. When
  the functions move to `juggle_cmd_agents_lifecycle.py`, they read from that
  module's namespace, not `juggle_cmd_agents`'s. Fix = pass params or update patches.
  Deferred to Phase 3 when internal imports will be migrated anyway.

- Phase 2.5 hooks split deferred: `juggle_hooks.py` handlers all read module-level
  globals (`DB_PATH`, `_CHECKPOINT_PATH`, `AUTOPILOT_FLAG`, `_CHECKPOINT_MAX_AGE_SECS`)
  defined in the entry-point file. Sub-modules would need a `hooks_config.py` to
  import these constants without circularity. Deferred to Phase 3.

## Follow-ups (deferred behavior changes)

- Unify pidfile kill semantics (monitor: group-kill+wait vs watchdog: single
  SIGTERM) — Phase 1 preserves each call-site's behavior via flags; unification is
  a deliberate behavior change deferred out of this refactor.
- `juggle_db_migrations.py` (532 lines) — grandfathered as a single ordered sequence
  of 34 migrations. Could be split into batches (migrations 1-17 / 18-34) but the
  benefit is marginal since the file is read once at startup. Revisit if it grows
  past 700 lines.
- Phase 3 prerequisite for cmd_agents + hooks splits: extract module-level globals
  that are patched by tests into explicit parameters or a shared constants module.
