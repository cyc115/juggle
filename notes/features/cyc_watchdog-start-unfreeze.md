# Watchdog lifecycle fixes — `juggle start` unfreezes + unify launchers

Branch: `cyc_watchdog-start-unfreeze` (off `origin/main`)
Worktree: `.claude/worktrees/agent-a85e8da5fc57789bc`

## Phase 1 (PRIMARY): `juggle start` starts + unfreezes the watchdog

**Problem.** `cmd_start` (`src/juggle_cmd_threads.py`) activates the session but
never touches the watchdog. There is NO CLI path to clear the freeze sentinel set
by `stop-watchdog --freeze` — only the cockpit W/R hotkeys
(`toggle_watchdog`/`restart_watchdog`) call `unfreeze_watchdog`. A CLI user who
freezes is stranded.

**Fix.** `cmd_start` clears the freeze sentinel (`unfreeze_watchdog(db_path)`) AND
ensures the watchdog is up (`ensure_watchdog(db_path, force=True, ...)`), reusing
the singleton primitives. `juggle start` becomes the CLI start/unfreeze path.
`--freeze` help text: tighten to name `juggle start`.

**Seam.** db path = `str(db.db_path)` (same as cockpit `_watchdog_db_path`).
`ensure_watchdog(..., spawn=...)` is the injectable seam the singleton tests use.
Extract a `_start_watchdog_for_cmd_start(db)` helper so tests can patch ONE seam.

**Regression pin** (`tests/test_cmd_start_unfreeze.py`): with the freeze sentinel
set, `cmd_start` clears it and the watchdog ends up alive — via injected `spawn=`,
no real daemon.

## Phase 2 (RCA P2): unify the two prod watchdog launchers onto the flock

**Problem.** TWO uncoordinated launchers:
- `juggle_cmd_threads._start_watchdog` (pidfile path, `_watchdog_pid_file`)
- `start_watchdog_detached`/`ensure_watchdog` (flock path, the singleton)

The pidfile-vs-flock split = each launcher has its own notion of "singleton", so
daemons "kill previous instance" repeatedly.

**Finding (pre-change):** `_start_watchdog` has NO production callers — only tests
+ the `_stop_watchdog` comment reference it. `cmd_start` does NOT call it (comment
at old line 210 says the cockpit owns the watchdog). So the pidfile launcher is
already dead in prod; Phase 2 removes the dead second-launcher code so the flock
is the ONE coordination primitive.

**Fix.** Remove dead pidfile launcher code (`_start_watchdog`, `_watchdog_pid_file`)
and migrate `cmd_stop`'s `_stop_watchdog()` to the flock `stop_watchdog`. Remove
the now-obsolete pidfile tests in `test_cmd_threads.py`; replace with a pin that
asserts no second pidfile launcher exists.

**Regression pin:** only one launch path remains; no second pidfile-based
singleton.

## Verification
- New pins RED-before / GREEN-after.
- Full `uv run pytest -q` with quarantine deselects:
  `--deselect tests/test_loc_gate.py --deselect tests/test_data_migration.py --deselect tests/test_integrate.py`
- `doctor --dry-run` on a tmp DB.
- Bump `version` in `.claude-plugin/plugin.json` (patch P1-only / minor if P2 lands).
- TODO Done entry.

## Notes / decisions
- RCA file `research/2026-06-20-watchdog-daemon-leak-rca.md` referenced by the task
  does NOT exist in the checkout. Proceeded from the §P2 summary in the task brief.
- Working in the agent's own git worktree (sandbox); branch renamed from the
  throwaway `worktree-agent-*` to `cyc_watchdog-start-unfreeze`.
