# Parallel-Safety Audit (speedup-tier, 2026-06-21)

Classification of every shared-resource test family under `pytest -n auto`.
This axis (serial vs parallel-safe) is INDEPENDENT of the slow/fast marker tier.

**Result:** the full suite (`-m "not watchdog_proc"`) runs GREEN and pass-count
stable under `pytest -n auto --dist loadgroup` on a 10-core host (~45–70s). No
test corrupts shared STATE under parallelism — DB-isolation + tmux-worker-keying
+ the hermetic prod-artifact guard cover that. The only `-n auto` failures found
were **load-timing flakes** in REAL-`uv run`-subprocess tests whose wall-clock
deadlines are exceeded when many cold-starts saturate the cores at once (NOT
state races). Two mitigations, both applied:

1. **`serial` group** for the uv-run CLI/daemon shell-out modules (below) so at
   most one heavy uv-run subprocess test runs at a time — honored via
   `--dist loadgroup` (now load-bearing).
2. **Poll-until-changed + bumped deadlines** for the real-PTY cockpit
   (`test_juggle_smoke.py`) and the daemon-liveness wait — load-tolerant in-test
   (serial-grouping the PTY test made it WORSE: it then always ran alongside
   `test_integrate` on one worker at peak load).

| Family | Shared resource | Class | Isolation mechanism / serial reason |
|---|---|---|---|
| All DB tests | `JUGGLE_DB_PATH` default DB | (1) safe | conftest `_isolate_db_from_prod` → per-`tmp_path` DB + prod-open `_connect` raise. xdist gives each worker its own tmp base; `JUGGLE_DB_PATH` is an env var so it propagates to `uv run` subprocess children. |
| Watchdog lock/pidfile | `lock_path_for(db_path)` / `write_singleton_pid` | (1) safe | Lock path derives from the tmp DB path → per-test isolated. Prod lock/pidfile additionally guarded by the autouse `_guard_no_prod_artifacts` per-seam wrapper (hermetic; fails loud BEFORE any prod write). |
| `tests/watchdog/*` (real tmux) | tmux session `juggle-watchdog-test` | (1) safe | Worker-keyed: `watchdog_session_name()` = `juggle-watchdog-test-{worker_id}`. Verified green under `-n 4` (201 passed) with no pane theft. |
| `test_tmux_lifecycle/submission/send_message`, `test_oneshot_observability` | tmux | (1) safe | `JUGGLE_TMUX_MOCK_*` / mocked subprocess — no real tmux. |
| Mock HTTP servers (`test_juggle_hindsight`, `test_juggle_cli_memory`) | TCP port | (1) safe | `HTTPServer(("127.0.0.1", 0))` → OS-assigned ephemeral port → no cross-worker collision. |
| `test_juggle_smoke.py`, `test_graph_dispatch.py`, `test_cmd_graph.py`, `test_ensure_watchdog_debounce.py`, `test_watchdog_daemon_main_entry.py`, `test_integrate.py` (`uv run` shell-outs) | child-process DB | (1) safe | Child inherits the per-test `JUGGLE_DB_PATH` from env (monkeypatch.setenv propagates to subprocess). |
| `tests/schedule/*` dry-run samples | fixed `/tmp/schedule-*-sample-*.md` | (1) safe (hardened M1) | Was a fixed `/tmp` path (no active race — distinct filenames, one writer each — but guard-blind + stale-file false-green risk). Now routed through `common.dry_run_sample_path()` (env `JUGGLE_SCHEDULE_SAMPLE_DIR`); the three dry-run tests point it at a fresh `tmp_path/samples`. |
| `watchdog_proc`-marked | host canonical watchdog | n/a | DESELECTED by default `addopts` (`-m 'not watchdog_proc'`) — never runs in the suite (2026-06-16 incident). |
| Real-daemon spawn (cockpit on_mount, `cmd_start`) | `uv run …daemon.py` child | (1) safe (hardened 2026-06-21) | Autouse `_no_real_watchdog_daemon_spawn` sets `JUGGLE_WATCHDOG_DISABLE_SPAWN=1` (inherited by `uv run` subprocess cockpit/CLI children) → `ensure_watchdog` never launches a real daemon. Autouse `_guard_no_leaked_watchdog_daemons` fails+SIGKILLs any survivor holding a lock under the test's own `tmp_path` (scoped — never blames a foreign worker / prod). See below. |

## Serial group (load-flake mitigation, applied in tests/conftest.py)

`pytest_collection_modifyitems` routes these REAL-`uv run`-subprocess modules to
`xdist_group("serial")` (+ `serial` marker) so at most one heavy uv-run cold-start
runs at a time — NOT because they corrupt shared state, but because concurrent
cold-starts saturate the cores and trip their wall-clock deadlines:

| Module | Why serial |
|---|---|
| `test_watchdog_daemon_main_entry.py` | spawns `uv run …daemon.py`, 15→30s liveness wait |
| `test_graph_dispatch.py`, `test_cmd_graph.py`, `test_ensure_watchdog_debounce.py` | `uv run` CLI shell-outs |
| `test_integrate.py` | `uv run` + heavy git subprocess work |

`test_juggle_smoke.py` (real cockpit PTY) is deliberately NOT serial-grouped —
grouping made it WORSE (it then always ran beside `test_integrate` at peak load).
Its two interactive frame-compare tests instead **poll-until-changed**
(`_frame_until_changed`) so they tolerate a lagging cockpit subprocess.

`--dist loadgroup` is REQUIRED for the serial grouping to work (it routes a group
to one worker); dropping it scatters the group and the load-flake can return —
see Rollback below.

## Daemon-survivor guard + spawn neutralizer (2026-06-21 daemon-teardown leak)

The always-full-suite (v1.80) surfaced a real-daemon leak the `watchdog_active`
teardown hardening (v1.77) never covered: `CockpitApp.on_mount` self-heals a
detached watchdog via `ensure_watchdog` → a REAL
`uv run python src/juggle_watchdog_daemon.py` against the test's tmp DB. So every
cockpit test that drives the real app (`run_test`) launched a background daemon;
when teardown reaped only the `uv run` parent the detached python CHILD orphaned
and kept ticking (8 full-suite ERRORS; observed live 2026-06-20 on a doctor
agent's full-suite run). Two autouse layers in `tests/conftest.py` (detection in
`tests/_daemon_guard.py`, pinned in `tests/test_daemon_survivor_guard.py`):

1. **Spawn neutralizer** (`_no_real_watchdog_daemon_spawn`) — sets
   `JUGGLE_WATCHDOG_DISABLE_SPAWN=1`, which `ensure_watchdog` honors on its real
   path (`spawn is None`) so NO ensure path (cockpit on_mount, `cmd_start`)
   launches a real daemon. Set via `setenv` so it ALSO propagates to `uv run`
   subprocess children (the smoke / cli-memory tests run a real `juggle start` /
   cockpit subprocess that inherits `os.environ` — an in-process monkeypatch
   could not reach them). Unit tests that inject a fake `spawn=` are unaffected
   (only the real spawn-None path is gated); the `watchdog_proc` real-daemon
   canaries use their own spawn marker, not this seam, so they too are
   unaffected.
2. **Survivor guard** (`_guard_no_leaked_watchdog_daemons`) — after each test,
   reads the per-DB singleton-lock sidecars `.<db>.watchdog.lock` directly under
   the test's `tmp_path`, and fails (after SIGKILL-reaping) on any recorded PID
   that is still alive AND a `juggle_watchdog_daemon.py` process. Scoping by the
   `tmp_path` lock files means a concurrent xdist worker's daemon or the live
   PROD watchdog is never mis-attributed — the same per-seam, hermetic design as
   the prod-DB / prod-artifact / worktree guards. The one intentional real-daemon
   spawner (`test_watchdog_daemon_main_entry.py`) now reaps the whole process
   GROUP (`killpg`, SIGTERM→SIGKILL) so the detached child is reaped, not just
   the `uv run` parent.

Note on the `slow` tier: the de-clock of the `watchdog/` gap tests
(`test_watchdog_active.py` — poll-until-marker + DB-backdated stall, no
wall-clock) landed earlier; it removed the FLAKINESS but the modules stay
real-tmux (not CPU-fast), so they REMAIN in `_SLOW_MODULE_SUFFIXES` (the
opt-in fast loop still skips them). They run in the default/integrate FULL suite.

## Rollback (M4) — if CI flakes under parallelism

`-n auto` lives in the integrate/CI `test_cmd` (config), NOT in `addopts`, so
disabling parallelism is a **config-only** change with no code edit:

1. **Disable parallelism:** drop `-n auto --dist loadgroup` from `test_cmd` →
   `uv run pytest -m "not watchdog_proc"` (still the FULL suite, just serial).
2. Because `addopts` does NOT carry `not slow` (B2), that serial command already
   runs the full suite — no extra `-m` override needed to re-include slow tests.
3. There is nothing else to revert: the `slow` marker only gates the opt-in
   `make test-fast` inner loop, never the default/integrate run.
