"""Tests for watchdog daemon fixes: supervised-reload guard, pidfile prune,
cockpit-supervision design.

Regression pin: 2026-06-14 watchdog-stay-alive.
  - Unsupervised daemon must NOT sys.exit on source staleness (it dies permanently).
  - Dead-PID watchdog-*.pid files must be pruned on startup.
  - Watchdog is cockpit-supervised (not launchd); plist is absent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ── 1. should_exit_for_reload ─────────────────────────────────────────────────


def test_should_exit_for_reload_supervised_stale_returns_true():
    """When supervised and source is stale, should signal exit for restart."""
    from juggle_watchdog_restart import should_exit_for_reload

    assert should_exit_for_reload(stale=True, supervised=True) is True


def test_should_exit_for_reload_unsupervised_stale_returns_false():
    """REGRESSION PIN 2026-06-14: unsupervised daemon must NOT exit on staleness.

    Pre-fix: the main loop called sys.exit(0) unconditionally when source was
    stale. Without a supervisor this killed the daemon permanently on every
    juggle source edit / git integrate touch.
    """
    from juggle_watchdog_restart import should_exit_for_reload

    assert should_exit_for_reload(stale=True, supervised=False) is False


def test_should_exit_for_reload_not_stale_returns_false():
    """When source is not stale, never exit regardless of supervised flag."""
    from juggle_watchdog_restart import should_exit_for_reload

    assert should_exit_for_reload(stale=False, supervised=True) is False
    assert should_exit_for_reload(stale=False, supervised=False) is False


# ── 2. pidfile prune ─────────────────────────────────────────────────────────


def _write_pid(path: Path, pid: int) -> None:
    path.write_text(str(pid))


def test_prune_stale_watchdog_pidfiles_removes_dead_pid(tmp_path):
    """Dead-PID watchdog-*.pid files are removed; live-PID files are kept."""
    from juggle_watchdog_daemon import prune_stale_watchdog_pidfiles

    dead_pid = 999999  # guaranteed non-existent
    live_pid = os.getpid()

    dead_file = tmp_path / "watchdog-dead-1111.pid"
    live_file = tmp_path / "watchdog-live-2222.pid"
    _write_pid(dead_file, dead_pid)
    _write_pid(live_file, live_pid)

    prune_stale_watchdog_pidfiles(tmp_path)

    assert not dead_file.exists(), "dead-PID pidfile must be pruned"
    assert live_file.exists(), "live-PID pidfile must be kept"


def test_prune_stale_watchdog_pidfiles_ignores_non_watchdog_files(tmp_path):
    """Non-watchdog files in the dir are never touched."""
    from juggle_watchdog_daemon import prune_stale_watchdog_pidfiles

    monitor = tmp_path / "monitor.pid"
    other = tmp_path / "somefile.pid"
    monitor.write_text("999999")
    other.write_text("999999")

    prune_stale_watchdog_pidfiles(tmp_path)

    assert monitor.exists(), "monitor.pid must not be touched"
    assert other.exists(), "non-watchdog pid file must not be touched"


def test_prune_stale_watchdog_pidfiles_handles_corrupt_pidfile(tmp_path):
    """Corrupt (non-integer) pidfile is treated as dead and removed."""
    from juggle_watchdog_daemon import prune_stale_watchdog_pidfiles

    bad = tmp_path / "watchdog-corrupt.pid"
    bad.write_text("not-a-pid")

    prune_stale_watchdog_pidfiles(tmp_path)

    assert not bad.exists(), "corrupt watchdog pidfile must be pruned"


# ── 3. Cockpit-supervisor design pins (2026-06-14: launchd removed) ──────────
# launchd plist removed: watchdog is now a child of the cockpit process.
# These pins verify the new design contract.


def test_launchd_plist_absent():
    """Regression pin (2026-06-14): launchd plist must NOT exist — watchdog is cockpit-supervised."""
    plist_path = Path(__file__).resolve().parent.parent / "deploy" / "com.juggle.watchdog.plist"
    assert not plist_path.exists(), (
        "launchd plist found — it was intentionally removed. "
        "Watchdog is now a cockpit child process, not a launchd service."
    )


def test_should_exit_supervisor_gone_contract():
    """Regression pin: supervisor-gone check must be a pure function in the daemon."""
    from juggle_watchdog_daemon import should_exit_supervisor_gone
    assert should_exit_supervisor_gone(supervisor_pid_alive=False) is True
    assert should_exit_supervisor_gone(supervisor_pid_alive=True) is False


def test_start_watchdog_child_no_setsid(tmp_path, monkeypatch):
    """Regression pin (2026-06-14): watchdog must be a cockpit child (no start_new_session)."""
    from juggle_watchdog_health import start_watchdog_child

    mock_proc = MagicMock()
    mock_proc.pid = 55555
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return mock_proc

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    start_watchdog_child(
        pid_file=tmp_path / "watchdog.pid",
        heartbeat_path=tmp_path / "heartbeat",
        log_path=tmp_path / "watchdog.log",
        repo_root=tmp_path,
    )

    assert captured.get("start_new_session") is not True, (
        "start_new_session must not be True — watchdog dies with cockpit"
    )
