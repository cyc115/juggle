"""TDD: cockpit owns a lock-gated, DETACHED watchdog + W/R hotkeys
(T-cockpit-watchdog-owner).

The watchdog SINGLETON LOCK (T-test-isolation-watchdog-singleton) is the real
guarantee. The cockpit only ensures-exists one detached daemon on startup; W
toggles it, R restarts-from-main. Closing the cockpit must NOT kill the
watchdog (detached, not a child).

Temp DBs / dummy lock-holders only — never the prod DB, never a real long-lived
daemon without teardown.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SRC = str(Path(__file__).parent.parent / "src")
sys.path.insert(0, SRC)

from juggle_watchdog_singleton import (  # noqa: E402
    canonical_repo_path,
    ensure_watchdog,
    is_watchdog_alive,
    read_lock_pid,
    restart_watchdog,
    start_watchdog_detached,
    toggle_watchdog,
)


@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "juggle.db")


# A detached process that grabs the DB's watchdog lock and sleeps — stands in
# for a live watchdog without running the real daemon loop.
def _spawn_lock_holder(db_path: str) -> int:
    code = (
        "import sys,time;"
        f"sys.path.insert(0,{SRC!r});"
        "from juggle_watchdog_singleton import acquire_singleton_lock;"
        f"acquire_singleton_lock({str(db_path)!r});"
        "time.sleep(30)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    # Wait until the lock is actually held.
    for _ in range(100):
        if is_watchdog_alive(db_path):
            return proc.pid
        time.sleep(0.02)
    raise AssertionError("lock holder did not acquire the lock in time")


def _kill(pid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        time.sleep(0.05)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return


@pytest.fixture
def reap():
    pids: list[int] = []
    yield pids
    for pid in pids:
        _kill(pid)


# ---------------------------------------------------------------------------
# Lock-based liveness
# ---------------------------------------------------------------------------


def test_is_watchdog_alive_false_when_none(db_path):
    assert is_watchdog_alive(db_path) is False


def test_is_watchdog_alive_true_when_holder(db_path, reap):
    reap.append(_spawn_lock_holder(db_path))
    assert is_watchdog_alive(db_path) is True
    assert read_lock_pid(db_path) == reap[0]


# ---------------------------------------------------------------------------
# ensure-exists: lock-gated, exactly one
# ---------------------------------------------------------------------------


def test_ensure_starts_one_when_none(db_path, reap):
    spawned: list[int] = []

    def fake_spawn(dbp, *, repo_path=None):
        pid = _spawn_lock_holder(dbp)
        spawned.append(pid)
        reap.append(pid)
        return pid

    started = ensure_watchdog(db_path, spawn=fake_spawn)
    assert started is True
    assert is_watchdog_alive(db_path) is True
    assert len(spawned) == 1


def test_ensure_noop_when_live(db_path, reap):
    reap.append(_spawn_lock_holder(db_path))  # already one live (lock held)

    calls: list[int] = []
    started = ensure_watchdog(
        db_path, spawn=lambda dbp, *, repo_path=None: calls.append(1)
    )
    assert started is False
    assert calls == []  # never spawned a second
    assert is_watchdog_alive(db_path) is True


# ---------------------------------------------------------------------------
# W toggle
# ---------------------------------------------------------------------------


def test_toggle_down_to_up_then_up_to_down(db_path, reap):
    def fake_spawn(dbp, *, repo_path=None):
        pid = _spawn_lock_holder(dbp)
        reap.append(pid)
        return pid

    # down -> up: acquires lock
    assert toggle_watchdog(db_path, spawn=fake_spawn) == "started"
    assert is_watchdog_alive(db_path) is True

    # up -> down: releases lock
    assert toggle_watchdog(db_path, spawn=fake_spawn) == "stopped"
    assert is_watchdog_alive(db_path) is False


# ---------------------------------------------------------------------------
# R restart
# ---------------------------------------------------------------------------


def test_restart_replaces_pid_keeps_singleton(db_path, reap):
    old = _spawn_lock_holder(db_path)
    reap.append(old)

    def fake_spawn(dbp, *, repo_path=None):
        pid = _spawn_lock_holder(dbp)
        reap.append(pid)
        return pid

    restart_watchdog(db_path, spawn=fake_spawn)

    new = read_lock_pid(db_path)
    assert new is not None and new != old
    # old pid gone
    with pytest.raises(ProcessLookupError):
        os.kill(old, 0)
    # still exactly one (lock held by the new one)
    assert is_watchdog_alive(db_path) is True


# ---------------------------------------------------------------------------
# Detachment — survives a simulated cockpit exit
# ---------------------------------------------------------------------------


def test_holder_is_detached_own_session(db_path, reap):
    pid = _spawn_lock_holder(db_path)
    reap.append(pid)
    # A detached process leads its OWN session (start_new_session): closing the
    # cockpit (a different session) cannot take it down via process-group signal.
    assert os.getsid(pid) == pid
    assert os.getsid(pid) != os.getsid(0)


def test_start_watchdog_detached_command_is_sanctioned_and_canonical(
    db_path, monkeypatch
):
    captured = {}

    class _FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    pid = start_watchdog_detached(db_path, repo_path="/canonical/juggle")
    assert pid == 4242
    kw = captured["kwargs"]
    assert kw["start_new_session"] is True, "watchdog must be detached"
    assert kw["cwd"] == "/canonical/juggle", "must launch from canonical main path"
    env = kw["env"]
    assert env["JUGGLE_DB_PATH"] == db_path
    assert env["JUGGLE_WATCHDOG_SANCTIONED"] == "1"


def test_canonical_repo_path_is_a_git_root(tmp_path):
    # The current repo's canonical path resolves to a real git work-tree root.
    p = canonical_repo_path()
    assert p
    assert (Path(p) / ".git").exists()


# ---------------------------------------------------------------------------
# Cockpit wiring
# ---------------------------------------------------------------------------


def test_cockpit_has_watchdog_hotkeys():
    from juggle_cockpit import CockpitApp

    actions = {b.action: b.key for b in CockpitApp.BINDINGS}
    assert actions.get("watchdog_toggle") == "w"
    assert "watchdog_restart" in actions.values() or any(
        b.action == "watchdog_restart" for b in CockpitApp.BINDINGS
    )


@pytest.mark.asyncio
async def test_cockpit_startup_ensures_watchdog_and_does_not_kill_on_exit(
    tmp_path, monkeypatch
):
    from juggle_db import JuggleDB
    import juggle_cockpit

    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    db.set_active(True)

    ensure_calls: list = []
    stop_calls: list = []
    monkeypatch.setattr(
        juggle_cockpit, "ensure_watchdog",
        lambda dbp, **kw: ensure_calls.append(dbp) or True,
        raising=False,
    )
    monkeypatch.setattr(
        juggle_cockpit, "stop_watchdog",
        lambda dbp, **kw: stop_calls.append(dbp),
        raising=False,
    )

    app = juggle_cockpit.CockpitApp(db_path=str(tmp_path / "juggle.db"))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)

    assert ensure_calls, "cockpit startup must ensure-exists the watchdog"
    # Detached: closing the cockpit must NOT stop the watchdog.
    assert stop_calls == [], "cockpit exit must NOT kill the detached watchdog"
