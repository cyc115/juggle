"""Global test isolation (2026-06-16 prod-DB pollution incident).

Two fail-closed guards applied to EVERY test:

  1. `JUGGLE_DB_PATH` is pointed at a per-test temp DB, so any bare `JuggleDB()`
     (or product code that opens the default DB) lands in a throwaway file —
     never the production DB at ~/.claude/juggle/juggle.db.

  2. `JuggleDB._connect` is wrapped to RAISE the instant any test opens the real
     production DB. Prod access in a test is then an immediate, loud error
     rather than silent pollution.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

_PROD_DB = (Path.home() / ".claude" / "juggle" / "juggle.db").resolve()


@pytest.fixture(autouse=True)
def _isolate_db_from_prod(tmp_path, monkeypatch):
    # 1. Redirect the default DB path at this test's temp dir, and initialize it
    #    so any `get_db()` / bare `JuggleDB()` with no explicit path resolves to
    #    a WORKING isolated DB (never the production DB).
    temp_db = tmp_path / "juggle-test.db"
    monkeypatch.setenv("JUGGLE_DB_PATH", str(temp_db))

    # 2. Fail-closed guard: opening the real prod DB is an immediate error.
    from juggle_db import JuggleDB

    _orig_connect = JuggleDB._connect

    def _guarded_connect(self):
        try:
            resolved = Path(self.db_path).resolve()
        except OSError:
            resolved = Path(self.db_path)
        if resolved == _PROD_DB:
            raise RuntimeError(
                "TEST ISOLATION VIOLATION: a test tried to open the production "
                f"DB {self.db_path}. Use a temp DB (tmp_path / JUGGLE_DB_PATH)."
            )
        return _orig_connect(self)

    monkeypatch.setattr(JuggleDB, "_connect", _guarded_connect)

    # Pre-initialize the isolated DB so default get_db() handles are usable.
    JuggleDB(str(temp_db)).init_db()
    yield


# ── Worktree-leak guard (2026-06-20 incident) ─────────────────────────────────
# A test that called ``_create_worktree`` WITHOUT pointing ``worktree_root`` at
# its own ``tmp_path`` wrote the checkout to /private/tmp/juggle-* — outside
# pytest's managed temp dir — so it was never cleaned up. 100+ orphaned dangling
# worktrees accumulated (pruned manually 2026-06-20).
#
# This guard is HERMETIC: it wraps ``_create_worktree`` at its definition module
# and asserts, per call, that the requested ``worktree_root`` resolves under the
# test's ``tmp_path``. It only inspects calls the test under test actually makes,
# so it is immune to concurrent juggle processes (watchdog, other agents) that
# also touch the shared /tmp — a global-filesystem snapshot guard misattributes
# those to innocent tests and flakes. Tests that monkeypatch _create_worktree
# (to mock it) transparently override the wrapper for that symbol — they create
# no real worktree, so they are unaffected.


@pytest.fixture(autouse=True)
def _guard_worktree_under_tmp(tmp_path, monkeypatch):
    import juggle_cmd_agents_worktree as _wt

    _orig_create = _wt._create_worktree
    _tmp_resolved = tmp_path.resolve()

    def _guarded_create(repo_path, thread_label, worktree_root):
        try:
            root_resolved = Path(worktree_root).resolve()
        except OSError:
            root_resolved = Path(worktree_root)
        # The worktree root must live under THIS test's tmp_path. A bare /tmp
        # (or any path outside tmp_path) is the leak this guard exists to stop.
        if _tmp_resolved not in (root_resolved, *root_resolved.parents):
            raise AssertionError(
                "TEST HYGIENE VIOLATION (2026-06-20 worktree-leak incident): "
                f"_create_worktree called with worktree_root={worktree_root!r} "
                f"which is NOT under this test's tmp_path ({tmp_path}). Pass "
                "worktree_root=str(tmp_path) so the checkout cannot leak into /tmp."
            )
        return _orig_create(repo_path, thread_label, worktree_root)

    # Patch the definition module and every module that re-exports the symbol,
    # so both ``from juggle_cmd_agents_worktree import _create_worktree`` and the
    # ``juggle_cmd_agents_common._create_worktree`` production seam are guarded.
    monkeypatch.setattr(_wt, "_create_worktree", _guarded_create)
    for _modname in ("juggle_cmd_agents_common", "juggle_cmd_agents"):
        _mod = sys.modules.get(_modname)
        if _mod is not None and hasattr(_mod, "_create_worktree"):
            monkeypatch.setattr(_mod, "_create_worktree", _guarded_create)
    yield


# ── Prod-artifact guard (speedup-tier xdist safety, 2026-06-21) ───────────────
# Under `-n auto` many workers run concurrently. `_isolate_db_from_prod` already
# RAISES on a prod-DB *connection*; this guard additionally fails any test that
# tries to WRITE the prod watchdog lock or a prod pidfile.
#
# B1 (critique): the plan's original design — a global st_mtime_ns snapshot of
# the prod DB/lock/pidfile before/after each test — was REJECTED. A live watchdog
# bumps exactly those mtimes every tick, so on the dogfooding dev machine (where
# the fast inner loop most needs to be reliable) a snapshot guard flakes whenever
# a test's window straddles a tick. Instead — mirroring the hermetic
# `_guard_worktree_under_tmp` seam wrapper above — we wrap the two functions that
# WRITE a prod artifact and assert, PER CALL, that the target is not a prod path,
# BEFORE any IO. This inspects only the calls the test under test actually makes,
# so it is immune to concurrent daemons.


@pytest.fixture(autouse=True)
def _guard_no_prod_artifacts(monkeypatch):
    import daemon_pidfile as _dp
    import juggle_watchdog_singleton as _wd
    from _xdist_isolation import assert_not_prod_artifact

    _orig_lock = _wd.acquire_singleton_lock

    def _guarded_lock(db_path):
        assert_not_prod_artifact(_wd.lock_path_for(db_path))
        return _orig_lock(db_path)

    _guarded_lock._prod_artifact_guarded = True

    _orig_pid = _dp.write_singleton_pid

    def _guarded_pid(pidfile, **kwargs):
        assert_not_prod_artifact(pidfile)
        return _orig_pid(pidfile, **kwargs)

    _guarded_pid._prod_artifact_guarded = True

    monkeypatch.setattr(_wd, "acquire_singleton_lock", _guarded_lock)
    monkeypatch.setattr(_dp, "write_singleton_pid", _guarded_pid)
    yield


@pytest.fixture
def _guard_no_prod_artifacts_active(_guard_no_prod_artifacts):  # noqa: ARG001
    """Expose the guard's activeness to a pin (the autouse guard yields None)."""
    return True


# ── Slow-tier marking (speedup-tier B2, 2026-06-21) ───────────────────────────
# Heavy modules (cockpit/Textual render, watchdog real-daemon/gap, `uv run`
# shell-outs) are marked `slow` so the OPT-IN inner loop
# (`make test-fast` / `-m 'not slow and not watchdog_proc'`) skips them. They STAY
# in the default/integrate FULL suite — `slow` is NEVER in global addopts (B2).
#
# Marking here in ONE place (vs a `pytestmark` line in 40+ modules) is a single
# source of truth and — critically — does NOT edit the watchdog test files that
# the unlanded 2026-06-20 watchdog-hardening plan also edits, avoiding a merge
# collision (M3). When that plan's frozen-clock de-clock lands and the watchdog
# gap tests become fast, drop their suffixes from _SLOW_MODULE_SUFFIXES below.

_SLOW_MODULE_SUFFIXES = (
    "test_juggle_smoke.py",
    "test_graph_dispatch.py",
    "test_cmd_graph.py",
    "test_ensure_watchdog_debounce.py",
    "test_watchdog_daemon_main_entry.py",
    "test_integrate.py",
    "test_watchdog_freeze.py",
    # watchdog real-daemon/gap modules (coordinate w/ 2026-06-20 plan — M3):
    "watchdog/test_baseline.py",
    "watchdog/test_watchdog_active.py",
    "watchdog/test_watchdog.py",
    "watchdog/test_hardening.py",
)

# Modules that spawn a REAL `uv run` Python subprocess (watchdog daemon / CLI
# shell-out) with a wall-clock deadline. Under `-n auto`, many such cold-starts
# at once saturate the cores and the deadlines flake (2026-06-21: a 15s
# daemon-liveness wait timed out ~1/3 of runs). Route them to one xdist_group so
# at most one heavy uv-run subprocess test runs at a time — load-flake removed
# without serializing the whole suite. Honored via `--dist loadgroup`.
#
# NOTE: test_juggle_smoke.py is deliberately NOT here. It drives a real cockpit
# PTY; serial-grouping made it WORSE (it then always ran alongside test_integrate
# on one worker, at peak load → 3/3 fail). Instead its interactive frame-compare
# tests poll-until-changed (load-tolerant in-test) and stay distributed.
_SERIAL_MODULE_SUFFIXES = (
    "test_graph_dispatch.py",
    "test_cmd_graph.py",
    "test_ensure_watchdog_debounce.py",
    "test_watchdog_daemon_main_entry.py",
    "test_integrate.py",
)


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    for item in items:
        path = str(item.fspath).replace(os.sep, "/")
        fname = path.rsplit("/", 1)[-1]
        if fname.startswith("test_cockpit_") or any(
            path.endswith(suffix) for suffix in _SLOW_MODULE_SUFFIXES
        ):
            item.add_marker(pytest.mark.slow)
        if any(path.endswith(suffix) for suffix in _SERIAL_MODULE_SUFFIXES):
            item.add_marker(pytest.mark.serial)
            item.add_marker(pytest.mark.xdist_group("serial"))
