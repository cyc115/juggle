# Incident: Watchdog G2 crash-loop (2026-06-13)

## Symptom
Watchdog daemon crash-looped on boot; armed/graph autopilot never ticked.
`~/.juggle/watchdog.log` showed repeating `SharedDBMigrationRefused` tracebacks
ending with "Watchdog stopped."

## Root cause
`juggle_watchdog_daemon.main()` calls `db.init_db()` → `assert_migration_allowed()`
→ `is_agent_context()`. The daemon was spawned while the launching shell's cwd
was inside a juggle agent worktree (`/tmp/juggle-juggle-*`). The G2 cwd heuristic
in `is_agent_context()` (`"juggle-juggle-" in cwd`) fired, returning True — causing
`assert_migration_allowed()` to raise `SharedDBMigrationRefused` for the watchdog's
own shared-DB `init_db()` call.

## Fix (v1.66.2)
1. **`src/dbops/graph_guards.py`**: Added `JUGGLE_ORCHESTRATOR=1` env-var check at
   the top of `is_agent_context()`. This flag is the authoritative orchestrator
   identity; it wins over the cwd heuristic AND `JUGGLE_IS_AGENT`. Only
   orchestrator/watchdog code sets it.

2. **`src/juggle_watchdog_daemon.py`**: Added `_set_orchestrator_preamble()` called
   at the very top of `main()` before any DB access. Sets `JUGGLE_ORCHESTRATOR=1`
   and `os.chdir(~/.juggle)` (belt-and-suspenders: chdir moves cwd off any worktree
   in case the env var is somehow unset).

3. Agent-refusal guard unchanged — real agents (JUGGLE_IS_AGENT=1, no orchestrator
   marker) are still refused. Regression-pinned by
   `test_g2_agent_refusal_still_enforced_without_orchestrator_marker`.
