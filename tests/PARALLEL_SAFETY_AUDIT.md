# Parallel-Safety Audit (speedup-tier, 2026-06-21)

Classification of every shared-resource test family under `pytest -n auto`.
This axis (serial vs parallel-safe) is INDEPENDENT of the slow/fast marker tier.

**Result:** the full suite (`-m "not watchdog_proc"`) ran GREEN under
`pytest -n auto --dist loadgroup` on the FIRST attempt — **2539 passed, 20
skipped in 45.17s** on a 10-core host — with **zero** parallel-unsafe failures.
No test required the `serial` / `xdist_group("serial")` fallback. The DB-isolation
+ tmux-worker-keying + hermetic prod-artifact guard already cover every shared
resource. `--dist loadgroup` is therefore present as a forward-compatible default
(honors any future `serial` group) but is not currently load-bearing.

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

## Serial fallback (none currently needed)

No test is `@pytest.mark.serial` / `@pytest.mark.xdist_group("serial")` as of
2026-06-21 — isolation covered every case, which is the preferred outcome (the
plan prefers isolation; serial is fallback-only). If a future test introduces
un-isolatable global state, mark it:

```python
pytestmark = [pytest.mark.serial, pytest.mark.xdist_group("serial")]
```

`--dist loadgroup` routes all `xdist_group("serial")` tests to ONE worker (run
sequentially among themselves) while the rest of the suite parallelizes.

## Rollback (M4) — if CI flakes under parallelism

`-n auto` lives in the integrate/CI `test_cmd` (config), NOT in `addopts`, so
disabling parallelism is a **config-only** change with no code edit:

1. **Disable parallelism:** drop `-n auto --dist loadgroup` from `test_cmd` →
   `uv run pytest -m "not watchdog_proc"` (still the FULL suite, just serial).
2. Because `addopts` does NOT carry `not slow` (B2), that serial command already
   runs the full suite — no extra `-m` override needed to re-include slow tests.
3. There is nothing else to revert: the `slow` marker only gates the opt-in
   `make test-fast` inner loop, never the default/integrate run.
