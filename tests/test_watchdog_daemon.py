"""Tests for watchdog daemon fixes: stale-code exit, pidfile prune,
cockpit-supervision design.

Regression pin: 2026-07-01 stale-daemon-activation-gap.
  - Daemon fingerprints plugin git HEAD at boot + each tick; on drift it exits
    cleanly REGARDLESS of supervisor (the cockpit respawns it on fresh code).
  - This supersedes the 2026-06-14 "unsupervised must NOT exit" pin: its premise
    (exit == permanent death) no longer holds now a respawn path exists.
  - Dead-PID watchdog-*.pid files must be pruned on startup.
  - Watchdog is cockpit-supervised (not launchd); plist is absent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ── 1. Stale-code exit (git-HEAD fingerprint) ─────────────────────────────────


def test_should_exit_for_stale_code_exits_on_drift():
    """REGRESSION PIN 2026-07-01: daemon ran 9h-stale code so merged fixes never activated.

    Pre-fix: the loop watched only juggle_watchdog.py's mtime and gated exit on
    ``should_exit_for_reload(stale, supervised)`` == (stale AND supervised); the
    UNSUPERVISED daemon therefore re-baselined and kept its import-time code
    indefinitely, so #5038/#5045 fixes merged to main never took effect until a
    manual restart (~9h stale overnight).

    Post-fix: the daemon fingerprints the plugin's git HEAD at boot and each tick;
    on ANY drift it exits cleanly REGARDLESS of supervisor — the cockpit's periodic
    ensure_watchdog respawns a fresh process on the new code. This supersedes the
    2026-06-14 pin ("unsupervised must NOT exit"), whose premise (exit ==
    permanent death) no longer holds now that a respawn path exists.
    """
    from juggle_watchdog_restart import should_exit_for_stale_code

    assert should_exit_for_stale_code("abc123", "def456") is True


def test_should_exit_for_stale_code_same_version_keeps_running():
    """No drift → never exit."""
    from juggle_watchdog_restart import should_exit_for_stale_code

    assert should_exit_for_stale_code("abc123", "abc123") is False


def test_should_exit_for_stale_code_unknown_version_fails_safe():
    """When either fingerprint is unknown (git unavailable), never exit — the
    respawn it would trigger can't be verified, so keep ticking on current code."""
    from juggle_watchdog_restart import should_exit_for_stale_code

    assert should_exit_for_stale_code(None, "def456") is False
    assert should_exit_for_stale_code("abc123", None) is False
    assert should_exit_for_stale_code(None, None) is False


def test_current_code_version_reads_git_head(tmp_path):
    """current_code_version returns the repo's git HEAD, changing as it advances."""
    import subprocess

    from juggle_watchdog_restart import current_code_version

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                       capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("1")
    git("add", "-A")
    git("commit", "-qm", "one")
    v1 = current_code_version(tmp_path)
    assert v1 and len(v1) >= 7
    (tmp_path / "f.txt").write_text("2")
    git("add", "-A")
    git("commit", "-qm", "two")
    assert current_code_version(tmp_path) != v1


def test_current_code_version_non_repo_returns_none(tmp_path):
    """Outside a git repo the fingerprint is unknown (None) → fail-safe no-exit."""
    from juggle_watchdog_restart import current_code_version

    assert current_code_version(tmp_path) is None


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
