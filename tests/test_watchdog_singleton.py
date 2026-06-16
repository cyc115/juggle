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
import signal
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


@pytest.mark.watchdog_proc
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


@pytest.mark.watchdog_proc
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


# ---------------------------------------------------------------------------
# Process-scope isolation (T-watchdog-test-proc-scope, 2026-06-16 incident):
# a default `pytest` run must NEVER touch host watchdog processes — it kept
# taking down the live canonical watchdog during the harness-gate full-suite run.
# ---------------------------------------------------------------------------

_THIS_FILE = os.path.relpath(
    __file__, os.path.join(os.path.dirname(__file__), "..")
)
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_proc_spawning_tests_are_deselected_by_default():
    """The destructive proc-spawning watchdog tests must be OPT-IN: a default
    `pytest` run does not collect them (so it never pkills host watchdogs); they
    are collected only under `-m watchdog_proc`."""
    default = subprocess.run(
        [sys.executable, "-m", "pytest", _THIS_FILE,
         "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert "test_terminate_all_watchdogs_kills_multiple" not in default.stdout, (
        f"destructive test collected in DEFAULT run:\n{default.stdout}"
    )
    assert "test_spawned_fixture_is_reaped" not in default.stdout

    optin = subprocess.run(
        [sys.executable, "-m", "pytest", _THIS_FILE,
         "-m", "watchdog_proc", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert "test_terminate_all_watchdogs_kills_multiple" in optin.stdout, (
        f"opt-in -m watchdog_proc did not collect the destructive tests:\n{optin.stdout}"
    )
    assert "test_spawned_fixture_is_reaped" in optin.stdout


def test_terminate_all_watchdogs_logic_is_mocked_no_real_procs(monkeypatch):
    """Kill-all logic unit-tested against a MOCKED process list — no real
    processes are spawned or signalled. SIGTERM goes to every reported pid; a
    pid still alive after the grace window is escalated to SIGKILL."""
    monkeypatch.setattr(ws, "find_watchdog_pids", lambda pattern=None: [4242, 4243])

    sent: list[tuple[int, int]] = []
    alive = {4243}  # 4243 ignores SIGTERM → must be escalated to SIGKILL

    def fake_kill(pid, sig):
        sent.append((pid, sig))
        if sig == 0:                       # liveness probe
            if pid not in alive:
                raise ProcessLookupError(pid)
            return
        if sig == signal.SIGTERM and pid == 4242:
            pass                           # 4242 dies on SIGTERM
        if sig == signal.SIGKILL:
            alive.discard(pid)

    monkeypatch.setattr(os, "kill", fake_kill)

    killed = ws.terminate_all_watchdogs("any-marker", timeout=0.2)

    assert killed == [4242, 4243]
    assert (4242, signal.SIGTERM) in sent
    assert (4243, signal.SIGTERM) in sent
    assert (4243, signal.SIGKILL) in sent     # escalation fired
    assert (4242, signal.SIGKILL) not in sent  # already dead → not re-killed


def test_find_watchdog_pids_parses_pgrep_and_excludes_self(monkeypatch):
    """find_watchdog_pids parses pgrep output and never returns our own pid —
    exercised with a MOCKED pgrep (no real process enumeration)."""
    me = os.getpid()
    fake_out = f"111\n222\n{me}\n"

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=fake_out, stderr="")

    monkeypatch.setattr(ws.subprocess, "run", fake_run)
    pids = ws.find_watchdog_pids("marker")
    assert pids == [111, 222]
    assert me not in pids


def test_default_run_does_not_kill_canonical_watchdog():
    """End-to-end: simulate the live canonical watchdog (a process whose cmdline
    carries the watchdog pattern + the sanctioned marker) and prove it SURVIVES a
    full default-selected run of this module — destructive proc tests are
    deselected, so nothing on the host is killed."""
    sentinel = f"juggle-agent-watchdog-{ws.SANCTION_ENV}-CANARY-{os.getpid()}"
    canary = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", sentinel]
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not ws.find_watchdog_pids(sentinel):
            time.sleep(0.05)
        assert ws.find_watchdog_pids(sentinel), "canary not visible to pgrep"

        # A default-selected run of THIS module (addopts deselects watchdog_proc).
        # Exclude the two meta-tests by name to avoid re-entrant nesting.
        run = subprocess.run(
            [sys.executable, "-m", "pytest", _THIS_FILE, "-q", "-p", "no:cacheprovider",
             "-k", "not canonical_watchdog and not deselected_by_default"],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
        assert canary.poll() is None, (
            "canary canonical watchdog was KILLED by the default run!\n"
            f"STDOUT:\n{run.stdout[-2000:]}\nSTDERR:\n{run.stderr[-1500:]}"
        )
    finally:
        canary.terminate()
        try:
            canary.wait(timeout=5)
        except subprocess.TimeoutExpired:
            canary.kill()
