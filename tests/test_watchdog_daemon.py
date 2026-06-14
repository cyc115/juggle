"""Tests for watchdog daemon fixes: supervised-reload guard, pidfile prune,
launchd plist validity.

Regression pin: 2026-06-14 watchdog-stay-alive.
  - Unsupervised daemon must NOT sys.exit on source staleness (it dies permanently).
  - Dead-PID watchdog-*.pid files must be pruned on startup.
  - launchd plist must be structurally valid with KeepAlive + RunAtLoad.
"""
from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path
from unittest.mock import patch

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


# ── 3. launchd plist ─────────────────────────────────────────────────────────

_PLIST_PATH = Path(__file__).resolve().parent.parent / "deploy" / "com.juggle.watchdog.plist"


def test_plist_file_exists():
    """launchd plist must exist at deploy/com.juggle.watchdog.plist."""
    assert _PLIST_PATH.exists(), f"plist not found at {_PLIST_PATH}"


def test_plist_parses_and_has_keepalive():
    """Plist must parse with plistlib and have KeepAlive=true."""
    with open(_PLIST_PATH, "rb") as f:
        data = plistlib.load(f)
    assert data.get("KeepAlive") is True, "KeepAlive must be True"


def test_plist_has_run_at_load():
    """Plist must have RunAtLoad=true."""
    with open(_PLIST_PATH, "rb") as f:
        data = plistlib.load(f)
    assert data.get("RunAtLoad") is True, "RunAtLoad must be True"


def test_plist_program_arguments_uses_uv():
    """ProgramArguments must invoke the daemon via uv run python."""
    with open(_PLIST_PATH, "rb") as f:
        data = plistlib.load(f)
    args = data.get("ProgramArguments", [])
    args_str = " ".join(args)
    assert "uv" in args_str, "ProgramArguments must use uv"
    assert "juggle_watchdog_daemon" in args_str or "juggle-agent-watchdog" in args_str, \
        "ProgramArguments must reference the daemon"


def test_plist_has_supervised_env():
    """Plist EnvironmentVariables must set JUGGLE_WATCHDOG_SUPERVISED=1."""
    with open(_PLIST_PATH, "rb") as f:
        data = plistlib.load(f)
    env = data.get("EnvironmentVariables", {})
    assert env.get("JUGGLE_WATCHDOG_SUPERVISED") == "1", \
        "EnvironmentVariables must include JUGGLE_WATCHDOG_SUPERVISED=1"
    assert env.get("JUGGLE_ORCHESTRATOR") == "1", \
        "EnvironmentVariables must include JUGGLE_ORCHESTRATOR=1"


def test_plist_has_log_paths():
    """Plist must define StandardOutPath and StandardErrorPath."""
    with open(_PLIST_PATH, "rb") as f:
        data = plistlib.load(f)
    assert "StandardOutPath" in data, "StandardOutPath missing"
    assert "StandardErrorPath" in data, "StandardErrorPath missing"
    # Both should point to the juggle log dir
    assert "juggle" in data["StandardOutPath"].lower() or \
           "watchdog" in data["StandardOutPath"].lower()
