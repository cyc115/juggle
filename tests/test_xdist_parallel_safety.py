"""Parallel-safety pins (speedup-tier, 2026-06-21).

Pins that the suite is xdist-safe: a stable per-worker id, the documented set of
prod artifacts the guard protects, and a HERMETIC per-seam prod-artifact guard
(critique B1: the plan's original global-mtime-snapshot guard was rejected — a
live watchdog bumps the prod DB/lock/pidfile mtimes every tick, so a snapshot
guard flakes on the dogfooding dev machine; instead we wrap the two functions
that WRITE a prod watchdog artifact and fail loud, per call, before any IO).
"""
from pathlib import Path

import pytest

from _xdist_isolation import (
    assert_not_prod_artifact,
    prod_artifact_paths,
    worker_id,
)


def test_worker_id_is_stable_string():
    """speedup-tier (2026-06-21): worker_id resolves to a non-empty token."""
    wid = worker_id()
    assert isinstance(wid, str) and wid


def test_prod_artifact_paths_include_db_and_lock():
    """speedup-tier (2026-06-21): the guard knows the prod DB + lock it must protect."""
    paths = prod_artifact_paths()
    prod_db = (Path.home() / ".claude" / "juggle" / "juggle.db").resolve()
    assert prod_db in paths
    # prod watchdog lock: .juggle.db.watchdog.lock next to the prod DB
    assert any(p.name == ".juggle.db.watchdog.lock" for p in paths)


def test_assert_not_prod_artifact_blocks_prod_allows_tmp(tmp_path):
    """speedup-tier (2026-06-21): the guard predicate raises on a prod artifact
    (DB / lock / a pidfile under ~/.juggle) and allows a tmp_path target. Pure,
    no IO — this is the hermetic decision the autouse guard makes per call."""
    prod_db = (Path.home() / ".claude" / "juggle" / "juggle.db").resolve()
    prod_lock = prod_db.parent / f".{prod_db.name}.watchdog.lock"
    prod_pidfile = Path.home() / ".juggle" / "watchdog.pid"
    for prod in (prod_db, prod_lock, prod_pidfile):
        with pytest.raises(AssertionError, match="prod"):
            assert_not_prod_artifact(prod)
    # A tmp_path artifact must NOT raise (assertion is not vacuous).
    assert_not_prod_artifact(tmp_path / "juggle-test.db") is None
    assert_not_prod_artifact(tmp_path / ".juggle-test.db.watchdog.lock") is None


def test_guard_fixture_is_active(_guard_no_prod_artifacts_active):
    """speedup-tier (2026-06-21): the autouse guard fixture ran for this test."""
    assert _guard_no_prod_artifacts_active is True


def test_autouse_guard_blocks_prod_lock_acquisition():
    """speedup-tier (2026-06-21): the autouse guard wraps acquire_singleton_lock
    and FAILS LOUD before any filesystem write if a test acquires the PROD
    watchdog lock — hermetic, per-call, immune to a live watchdog daemon (B1)."""
    import juggle_watchdog_singleton as s

    # Guard installed? (marker set by the autouse fixture's wrapper). Asserted
    # FIRST so a missing guard fails RED here — never reaching the prod-seam call.
    assert getattr(s.acquire_singleton_lock, "_prod_artifact_guarded", False), (
        "autouse _guard_no_prod_artifacts did not wrap acquire_singleton_lock"
    )
    with pytest.raises(AssertionError, match="prod"):
        s.acquire_singleton_lock(str(s.PROD_DB_PATH))


def test_autouse_guard_blocks_prod_pidfile_write():
    """speedup-tier (2026-06-21): the autouse guard wraps write_singleton_pid and
    blocks a write into the prod ~/.juggle dir before touching the filesystem."""
    import daemon_pidfile as dp

    assert getattr(dp.write_singleton_pid, "_prod_artifact_guarded", False), (
        "autouse _guard_no_prod_artifacts did not wrap write_singleton_pid"
    )
    prod_pidfile = Path.home() / ".juggle" / "watchdog.pid"
    with pytest.raises(AssertionError, match="prod"):
        dp.write_singleton_pid(prod_pidfile)


@pytest.mark.slow
@pytest.mark.xdist_group("serial")
def test_full_suite_parallel_stability_contract():
    """speedup-tier (2026-06-21): META-PIN documenting the -n auto acceptance
    contract — the full suite is green and pass-count stable under -n auto across
    reruns. The ENFORCING evidence is the integrate/CI run (full suite, -n auto);
    this pin asserts the contract's MECHANISM still exists so it cannot be
    silently dropped, without recursively spawning pytest inside pytest."""
    import tomllib

    root = Path(__file__).parent.parent
    ini = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    marker_names = [m.split(":", 1)[0] for m in ini["markers"]]
    # The two-axis parallel mechanism (slow tier + serial group) must persist.
    assert "slow" in marker_names and "serial" in marker_names
    # The audit deliverable classifying every shared-resource family must persist.
    assert (root / "tests" / "PARALLEL_SAFETY_AUDIT.md").exists()


def test_watchdog_session_name_is_worker_scoped(monkeypatch):
    """speedup-tier (2026-06-21, rewritten 2026-07-26 for the PID discriminator —
    see test_watchdog_session_name_is_process_scoped): two xdist workers must NOT
    share one real tmux session — the session name is keyed to
    PYTEST_XDIST_WORKER so parallel workers never steal each other's panes.
    Asserted via startswith (not exact-equality) because the name now also
    carries a PID suffix (non-deterministic across test runs)."""
    from _xdist_isolation import watchdog_session_name

    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    name = watchdog_session_name()
    assert name.startswith("juggle-watchdog-test-gw3-")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw7")
    name2 = watchdog_session_name()
    assert name2.startswith("juggle-watchdog-test-gw7-")
    assert name2 != name  # different worker -> different session


def test_watchdog_session_name_is_process_scoped(monkeypatch):
    """(2026-07-26 incident): two SEPARATE `pytest -n auto` processes running
    CONCURRENTLY (e.g. two overlapping `make test-integrate` runs) each spawn
    their OWN worker named 'gw0' — PYTEST_XDIST_WORKER is relative to its own
    invocation, not globally unique. The worker-scoped name alone therefore
    collided across processes: both gw0's computed the SAME tmux session name
    and stole each other's panes, reproducibly breaking
    tests/watchdog/test_watchdog_active.py + test_baseline.py under concurrent
    runs. os.getpid() is unique among all concurrently-running processes on the
    host, so the session name must also vary by it."""
    import os

    from _xdist_isolation import watchdog_session_name

    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setattr(os, "getpid", lambda: 11111)
    name_a = watchdog_session_name()
    monkeypatch.setattr(os, "getpid", lambda: 22222)
    name_b = watchdog_session_name()
    assert name_a != name_b, (
        "two concurrent pytest processes with the SAME xdist worker id (gw0) "
        "must not collide on the same tmux session name"
    )


def test_watchdog_conftest_uses_worker_scoped_session():
    """speedup-tier (2026-06-21): the watchdog conftest sources its session name
    from the worker-scoped helper (not the old fixed 'juggle-watchdog-test')."""
    import importlib.util

    here = Path(__file__).parent
    spec = importlib.util.spec_from_file_location(
        "_wd_conftest_probe", here / "watchdog" / "conftest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from _xdist_isolation import watchdog_session_name

    assert mod.test_session_name() == watchdog_session_name()
    assert mod.test_session_name().startswith("juggle-watchdog-test-")
