"""Watchdog singleton + prod-launch guard (2026-06-16 incident).

A coder running the watchdog suite from a worktree leaked a REAL watchdog
daemon that ran against the PRODUCTION DB and kept ticking independently of the
orchestrator. `stop-watchdog` only killed the recorded PID, so the rogue
survived a freeze.

Fixes pinned here:
  2a. The daemon refuses to run against the prod DB unless launched through the
      sanctioned orchestrator entrypoint (JUGGLE_WATCHDOG_SANCTIONED=1, which
      test runs never set).
  2b. The daemon takes an exclusive flock — a second instance refuses to start.
  3.  stop-watchdog terminates EVERY running watchdog process, not just the one
      recorded in the pidfile.
  4.  A spawned watchdog-like fixture is guaranteed-torn-down (no survivor).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import juggle_watchdog_singleton as ws  # noqa: E402


# ---------------------------------------------------------------------------
# 2a. Prod-launch sanction guard
# ---------------------------------------------------------------------------


def test_launch_refused_on_prod_without_sanction(monkeypatch):
    monkeypatch.delenv(ws.SANCTION_ENV, raising=False)
    with pytest.raises(ws.WatchdogLaunchRefused):
        ws.assert_launch_allowed(str(ws.PROD_DB_PATH))


def test_launch_allowed_on_prod_when_sanctioned(monkeypatch):
    monkeypatch.setenv(ws.SANCTION_ENV, "1")
    ws.assert_launch_allowed(str(ws.PROD_DB_PATH))  # must not raise


def test_launch_allowed_on_temp_db_regardless(monkeypatch, tmp_path):
    monkeypatch.delenv(ws.SANCTION_ENV, raising=False)
    ws.assert_launch_allowed(str(tmp_path / "juggle.db"))  # must not raise


# ---------------------------------------------------------------------------
# 2b. Exclusive singleton flock
# ---------------------------------------------------------------------------


def test_second_singleton_acquire_refuses(tmp_path):
    db = tmp_path / "juggle.db"
    fd = ws.acquire_singleton_lock(str(db))
    try:
        with pytest.raises(ws.WatchdogAlreadyRunning):
            ws.acquire_singleton_lock(str(db))
    finally:
        ws.release_singleton_lock(fd)


def test_singleton_reacquire_after_release(tmp_path):
    db = tmp_path / "juggle.db"
    fd = ws.acquire_singleton_lock(str(db))
    ws.release_singleton_lock(fd)
    fd2 = ws.acquire_singleton_lock(str(db))  # must not raise
    ws.release_singleton_lock(fd2)


def test_distinct_dbs_have_independent_locks(tmp_path):
    a = ws.acquire_singleton_lock(str(tmp_path / "a.db"))
    b = ws.acquire_singleton_lock(str(tmp_path / "b.db"))  # different DB → ok
    ws.release_singleton_lock(a)
    ws.release_singleton_lock(b)


# ---------------------------------------------------------------------------
# 3. stop-watchdog kills ALL watchdog processes
# ---------------------------------------------------------------------------


def _spawn_marked(marker: str) -> subprocess.Popen:
    # A harmless long sleep whose cmdline contains the marker so pgrep -f finds it.
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", marker]
    )


def test_terminate_all_watchdogs_kills_multiple():
    marker = "juggle-agent-watchdog-pytest-killall"
    procs = [_spawn_marked(marker) for _ in range(2)]
    try:
        # Wait for them to be visible to pgrep.
        deadline = time.monotonic() + 5
        want = {p.pid for p in procs}
        while time.monotonic() < deadline:
            if want <= set(ws.find_watchdog_pids(marker)):
                break
            time.sleep(0.05)
        assert want <= set(ws.find_watchdog_pids(marker))

        killed = ws.terminate_all_watchdogs(marker)
        assert want <= set(killed)

        # Both must be dead.
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            assert p.poll() is not None
        assert not ws.find_watchdog_pids(marker)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()


# ---------------------------------------------------------------------------
# 4. A spawned watchdog-like fixture leaves no survivor after teardown
# ---------------------------------------------------------------------------


def test_spawned_fixture_is_reaped():
    marker = "juggle-agent-watchdog-pytest-reap"
    p = _spawn_marked(marker)
    try:
        time.sleep(0.2)
        assert p.poll() is None
    finally:
        ws.terminate_all_watchdogs(marker)
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    assert p.poll() is not None
    assert not ws.find_watchdog_pids(marker)
