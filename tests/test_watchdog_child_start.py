"""TDD — watchdog_needs_start, start_watchdog_child, should_exit_supervisor_gone,
and cockpit on_unmount termination.

Written BEFORE implementation (RED phase).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# watchdog_needs_start — pure decision fn
# ---------------------------------------------------------------------------

def test_needs_start_pid_dead():
    from juggle_watchdog_health import watchdog_needs_start
    assert watchdog_needs_start(pid_alive=False, heartbeat_age_s=10) is True


def test_needs_start_pid_alive_fresh_heartbeat():
    from juggle_watchdog_health import watchdog_needs_start
    assert watchdog_needs_start(pid_alive=True, heartbeat_age_s=10) is False


def test_needs_start_pid_alive_stale_heartbeat():
    from juggle_watchdog_health import watchdog_needs_start
    assert watchdog_needs_start(pid_alive=True, heartbeat_age_s=100, threshold_s=90) is True


def test_needs_start_no_heartbeat():
    from juggle_watchdog_health import watchdog_needs_start
    assert watchdog_needs_start(pid_alive=True, heartbeat_age_s=None) is True


def test_needs_start_heartbeat_exactly_at_threshold():
    from juggle_watchdog_health import watchdog_needs_start
    # At exactly threshold — not stale yet
    assert watchdog_needs_start(pid_alive=True, heartbeat_age_s=90, threshold_s=90) is False


# ---------------------------------------------------------------------------
# start_watchdog_child — NO start_new_session (child of cockpit)
# ---------------------------------------------------------------------------

def test_start_watchdog_child_no_start_new_session(tmp_path, monkeypatch):
    """Popen must NOT pass start_new_session=True (child stays in same group)."""
    from juggle_watchdog_health import start_watchdog_child

    mock_proc = MagicMock()
    mock_proc.pid = 12345

    captured_kwargs = {}

    def fake_popen(cmd, **kwargs):
        captured_kwargs.update(kwargs)
        return mock_proc

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    pid_file = tmp_path / "watchdog.pid"
    heartbeat = tmp_path / "heartbeat"
    log_file = tmp_path / "watchdog.log"

    result = start_watchdog_child(
        pid_file=pid_file,
        heartbeat_path=heartbeat,
        log_path=log_file,
        repo_root=tmp_path,
    )

    assert result is mock_proc
    assert captured_kwargs.get("start_new_session") is not True, (
        "start_new_session must NOT be True — watchdog must die with cockpit"
    )


def test_start_watchdog_child_passes_supervisor_pid(tmp_path, monkeypatch):
    """JUGGLE_SUPERVISOR_PID env var must be set to the given supervisor_pid."""
    from juggle_watchdog_health import start_watchdog_child

    mock_proc = MagicMock()
    mock_proc.pid = 12345
    captured_env = {}

    def fake_popen(cmd, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        return mock_proc

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    pid_file = tmp_path / "watchdog.pid"
    heartbeat = tmp_path / "heartbeat"
    log_file = tmp_path / "watchdog.log"

    start_watchdog_child(
        pid_file=pid_file,
        heartbeat_path=heartbeat,
        log_path=log_file,
        repo_root=tmp_path,
        supervisor_pid=9999,
    )

    assert captured_env.get("JUGGLE_SUPERVISOR_PID") == "9999"


def test_start_watchdog_child_noop_when_live(tmp_path, monkeypatch):
    """No Popen call when a live watchdog already holds the pidfile + fresh heartbeat."""
    from juggle_watchdog_health import start_watchdog_child

    # Use own PID — guaranteed alive
    pid_file = tmp_path / "watchdog.pid"
    pid_file.write_text(str(os.getpid()))

    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()  # fresh heartbeat

    log_file = tmp_path / "watchdog.log"

    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)

    result = start_watchdog_child(
        pid_file=pid_file,
        heartbeat_path=heartbeat,
        log_path=log_file,
        repo_root=tmp_path,
    )

    mock_popen.assert_not_called()
    assert result is None


def test_start_watchdog_child_starts_when_heartbeat_stale(tmp_path, monkeypatch):
    """Starts a new process when pid is alive but heartbeat is stale."""
    from juggle_watchdog_health import start_watchdog_child

    pid_file = tmp_path / "watchdog.pid"
    pid_file.write_text(str(os.getpid()))  # alive PID

    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    # Make heartbeat appear stale
    old_time = time.time() - 200
    os.utime(heartbeat, (old_time, old_time))

    log_file = tmp_path / "watchdog.log"

    mock_proc = MagicMock()
    mock_proc.pid = 99999
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: mock_proc)

    result = start_watchdog_child(
        pid_file=pid_file,
        heartbeat_path=heartbeat,
        log_path=log_file,
        repo_root=tmp_path,
    )

    assert result is mock_proc


# ---------------------------------------------------------------------------
# should_exit_supervisor_gone — pure decision fn
# ---------------------------------------------------------------------------

def test_should_exit_supervisor_gone_returns_true_when_dead():
    from juggle_watchdog_daemon import should_exit_supervisor_gone
    assert should_exit_supervisor_gone(supervisor_pid_alive=False) is True


def test_should_exit_supervisor_gone_returns_false_when_alive():
    from juggle_watchdog_daemon import should_exit_supervisor_gone
    assert should_exit_supervisor_gone(supervisor_pid_alive=True) is False


# ---------------------------------------------------------------------------
# Cockpit on_unmount terminates watchdog child
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cockpit_unmount_terminates_watchdog(tmp_path, monkeypatch):
    """Cockpit on_unmount must terminate the watchdog child process."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    db.set_active(True)

    mock_proc = MagicMock()
    mock_proc.returncode = None  # still running
    mock_proc.wait = MagicMock(return_value=0)

    # Prevent actual Popen
    monkeypatch.setattr(
        "juggle_watchdog_health.start_watchdog_child",
        lambda **kw: mock_proc,
    )

    app = CockpitApp(db_path=str(tmp_path / "juggle.db"))
    async with app.run_test(size=(120, 40)) as pilot:
        # Inject mock proc directly
        app._watchdog_proc = mock_proc
        await pilot.pause(0.1)

    # After exit, terminate must have been called
    mock_proc.terminate.assert_called()
