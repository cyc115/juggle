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
