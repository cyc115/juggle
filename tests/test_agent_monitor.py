"""Tests for the agent-monitor polling logic and singleton hygiene.

Logic moved from scripts/juggle-agent-monitor to src/juggle_monitor_daemon.py
in the 2026-06-10 refactor; same assertions through the new seam (the script
is now a thin wrapper)."""

import importlib.util
import os
import signal
import sys
from pathlib import Path

import pytest

# Add src/ to path so juggle_settings imports work when module is loaded
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB

_SCRIPT = Path(__file__).parent.parent / "src" / "juggle_monitor_daemon.py"


def _load_monitor():
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("juggle_agent_monitor", str(_SCRIPT))
    spec = importlib.util.spec_from_loader("juggle_agent_monitor", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "juggle.db"))
    d.init_db()
    return d


def test_poll_detects_researcher_completion(db):
    mod = _load_monitor()

    tid = db.create_thread("smoke test researcher action item", session_id="s")
    db.update_thread(tid, title="smoke test researcher action item", status="closed")
    db.add_action_item(tid, message="Review: done", type_="review", priority="normal")
    nid = db.add_notification_v2(tid, "smoke test researcher action item: done", "s")

    with db._connect() as conn:
        conn.row_factory = __import__("sqlite3").Row
        lines, new_id = mod._poll_once(conn, last_seen_id=0)

    thread = db.get_thread(tid)
    label = thread["user_label"]
    assert [line for _, line in lines] == [
        f"[{label}] researcher: smoke test researcher action item"
    ]
    assert new_id == nid


def test_poll_detects_coder_completion(db):
    mod = _load_monitor()

    tid = db.create_thread("deploy feature X", session_id="s")
    db.update_thread(tid, title="deploy feature X", status="closed")
    nid = db.add_notification_v2(tid, "deploy feature X: merged", "s")

    with db._connect() as conn:
        conn.row_factory = __import__("sqlite3").Row
        lines, new_id = mod._poll_once(conn, last_seen_id=0)

    thread = db.get_thread(tid)
    label = thread["user_label"]
    assert [line for _, line in lines] == [f"[{label}] coder: deploy feature X"]
    assert new_id == nid


def test_poll_skips_non_closed_threads(db):
    mod = _load_monitor()

    # Notification for a still-running thread (mid-task notify)
    tid = db.create_thread("ongoing task", session_id="s")
    db.add_notification_v2(tid, "milestone: step 1 done", "s")

    with db._connect() as conn:
        conn.row_factory = __import__("sqlite3").Row
        lines, _ = mod._poll_once(conn, last_seen_id=0)

    assert lines == []


def test_poll_respects_last_seen_id(db):
    mod = _load_monitor()

    tid = db.create_thread("task", session_id="s")
    db.update_thread(tid, title="task", status="closed")
    nid1 = db.add_notification_v2(tid, "task: first", "s")
    nid2 = db.add_notification_v2(tid, "task: second", "s")

    with db._connect() as conn:
        conn.row_factory = __import__("sqlite3").Row
        # Only fetch from nid1 onward
        lines, new_id = mod._poll_once(conn, last_seen_id=nid1)

    assert len(lines) == 1
    assert new_id == nid2


# ---------------------------------------------------------------------------
# Singleton hygiene tests (mirroring test_watchdog_actionable_items.py pattern)
# ---------------------------------------------------------------------------


def test_kill_existing_monitor_skips_non_monitor_process(tmp_path, monkeypatch):
    """Must NOT kill a process whose cmdline does not contain 'juggle-agent-monitor'."""
    mod = _load_monitor()

    pidfile = tmp_path / "monitor.pid"
    pidfile.write_text("99999")

    killed: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            return  # process "exists"
        killed.append((pid, sig))

    monkeypatch.setattr("os.kill", fake_kill)
    monkeypatch.setattr(mod, "_is_monitor_process", lambda pid: False)

    mod._kill_existing_monitor_from_pidfile(pidfile)

    assert killed == [], "Must not kill a process that is not a monitor"


def test_kill_existing_monitor_kills_confirmed_monitor(tmp_path, monkeypatch):
    """Must send SIGTERM to a confirmed monitor process."""
    mod = _load_monitor()

    pidfile = tmp_path / "monitor.pid"
    pidfile.write_text("99999")

    killed: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if killed:
                raise ProcessLookupError
            return  # alive initially
        killed.append((pid, sig))

    monkeypatch.setattr("os.kill", fake_kill)
    monkeypatch.setattr(mod, "_is_monitor_process", lambda pid: True)

    mod._kill_existing_monitor_from_pidfile(pidfile)

    assert any(sig == signal.SIGTERM for _, sig in killed), (
        "Must send SIGTERM to confirmed monitor PID"
    )


def test_kill_existing_monitor_skips_own_pid(tmp_path, monkeypatch):
    """Must not kill itself even if pidfile contains our own PID."""
    mod = _load_monitor()

    pidfile = tmp_path / "monitor.pid"
    pidfile.write_text(str(os.getpid()))

    killed: list = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)) if sig != 0 else None)
    monkeypatch.setattr(mod, "_is_monitor_process", lambda pid: True)

    mod._kill_existing_monitor_from_pidfile(pidfile)

    assert not any(sig != 0 for _, sig in killed), "Must not kill own PID"


def test_kill_existing_monitor_handles_missing_pidfile(tmp_path):
    """Must handle gracefully when pidfile does not exist."""
    mod = _load_monitor()

    pidfile = tmp_path / "monitor.pid"
    # No write — file does not exist

    # Should not raise
    mod._kill_existing_monitor_from_pidfile(pidfile)


# ---------------------------------------------------------------------------
# Graceful-shutdown + cursor-durability pins
# Incident 2026-06-21: monitor SIGTERM leaves stale pidfile / risks dropped
# completion (atexit does not run on SIGTERM; in-memory cursor reset to MAX(id)
# on every restart skipped completions that arrived while the daemon was down).
# ---------------------------------------------------------------------------


import subprocess  # noqa: E402
import time  # noqa: E402


def test_sigterm_leaves_no_stale_pidfile(tmp_path):
    """2026-06-21: a SIGTERM to the running monitor must remove its own pidfile.

    Pre-fix the daemon cleaned up only via atexit, which does NOT run on
    SIGTERM (exit 143) — leaving a stale pidfile behind.
    """
    home = tmp_path / "home"
    home.mkdir()
    pidfile = home / ".juggle" / "monitor.pid"

    repo = Path(__file__).parent.parent
    boot = (
        "import sys; sys.path.insert(0, 'src'); "
        "from juggle_monitor_daemon import main; main()"
    )
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.Popen(
        [sys.executable, "-c", boot], env=env, cwd=str(repo),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            if pidfile.exists():
                break
            time.sleep(0.1)
        assert pidfile.exists(), "daemon never wrote its pidfile"

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)

        assert proc.returncode == 0, f"SIGTERM should exit cleanly, got {proc.returncode}"
        assert not pidfile.exists(), "stale pidfile remained after SIGTERM"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_cursor_baseline_skips_history_on_first_run(db, tmp_path):
    """First run (no cursor file) baselines at MAX(id) so history is not replayed."""
    mod = _load_monitor()
    cursor = tmp_path / "monitor.cursor"

    tid = db.create_thread("old", session_id="s")
    db.update_thread(tid, title="old", status="closed")
    nid = db.add_notification_v2(tid, "old: done", "s")

    start = mod._load_cursor(cursor, Path(db.db_path))
    assert start == nid, "first run must baseline at MAX(id), not replay history"
    assert cursor.exists(), "baseline cursor must be persisted"


def test_cursor_reemits_unconsumed_completion_across_restart(db, tmp_path):
    """2026-06-21: a completion present at SIGTERM time is re-emitted (not
    dropped, not duplicated) after a restart from the persisted cursor."""
    import sqlite3 as _sql

    mod = _load_monitor()
    cursor = tmp_path / "monitor.cursor"
    dbpath = Path(db.db_path)

    # Daemon started before any completion -> baseline cursor 0 (empty db).
    assert mod._load_cursor(cursor, dbpath) == 0

    # A completion arrives.
    tid = db.create_thread("task", session_id="s")
    db.update_thread(tid, title="task", status="closed")
    db.add_notification_v2(tid, "task: done", "s")

    def _poll():
        with db._connect() as conn:
            conn.row_factory = _sql.Row
            return mod._poll_once(conn, mod._load_cursor(cursor, dbpath))

    # Tick emits the line, but SIGTERM hits before the cursor is saved.
    lines, _ = _poll()
    assert len(lines) == 1

    # Restart: cursor never advanced -> completion is re-emitted (NOT dropped).
    lines2, new_id2 = _poll()
    assert len(lines2) == 1, "unconsumed completion must survive a restart"

    # Now durably deliver: save the cursor.
    mod._save_cursor(cursor, new_id2)

    # Restart again: cursor persisted -> NOT duplicated.
    lines3, _ = _poll()
    assert lines3 == [], "delivered completion must not be re-fired after restart"
