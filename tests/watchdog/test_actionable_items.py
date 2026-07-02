"""TDD tests for actionable-items refactor.

Rules under test:
  - Auto-recoverable event  → add_notification_v2, NOT add_action_item
  - Recovery exhausted      → add_action_item with [RQ] + Decide: + Cause:
  - Singleton               → second start kills watchdog PIDs only
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_cascade_state():
    import juggle_watchdog
    juggle_watchdog._cold_start_failures.clear()
    juggle_watchdog._cascade_filed.clear()
    juggle_watchdog._nudge_state.clear()
    yield
    juggle_watchdog._cold_start_failures.clear()
    juggle_watchdog._cascade_filed.clear()
    juggle_watchdog._nudge_state.clear()


def _dead_agent(watchdog_retried: int = 0) -> dict:
    return {
        "id": "a" * 32,
        "pane_id": "%5",
        "status": "busy",
        "assigned_thread": "thread-xyz",
        "role": "coder",
        "model": None,
        "last_task": "do the work",
        "watchdog_retried": watchdog_retried,
        "last_active": None,
    }


def _make_db_mock() -> MagicMock:
    db = MagicMock()
    db.get_agent.return_value = _dead_agent()
    db.get_thread.return_value = {"user_label": "my-thread", "label": "my-thread"}
    return db


def _make_mgr_mock(*, pane_exists: bool = False) -> MagicMock:
    mgr = MagicMock()
    mgr.verify_pane.return_value = pane_exists
    mgr.spawn_agent.return_value = {"id": "b" * 32, "pane_id": "%9"}
    return mgr


# ---------------------------------------------------------------------------
# 1. Transient first stall + successful auto-recovery → notification only
# ---------------------------------------------------------------------------


def test_first_stall_auto_recovery_emits_notification_not_action_item(tmp_path):
    """First stall (watchdog_retried=0), send_task succeeds → notification only."""
    from juggle_watchdog import execute_recovery

    db = _make_db_mock()
    db.get_agent.return_value = _dead_agent(watchdog_retried=0)
    mgr = _make_mgr_mock(pane_exists=False)  # dead pane → recovery proceeds

    execute_recovery(
        db, mgr, _dead_agent(watchdog_retried=0), "pane content",
        recovery_dir=tmp_path, session_id="sid",
    )

    # Must emit a notification for the auto-retry
    db.add_notification_v2.assert_called()
    # Must NOT file a blocking action item for an auto-recoverable event
    db.add_action_item.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Recovery exhausted (retry_blocked) → action item with [RQ] + Decide + Cause
# ---------------------------------------------------------------------------


def test_recovery_exhausted_action_item_has_rq_decide_cause(tmp_path):
    """retry_blocked → action item with [RQ] prefix, Decide:, and Cause:."""
    from juggle_watchdog import execute_recovery

    agent = _dead_agent(watchdog_retried=1)
    db = _make_db_mock()
    db.get_agent.return_value = agent
    mgr = _make_mgr_mock(pane_exists=False)

    execute_recovery(
        db, mgr, agent, "pane content",
        recovery_dir=tmp_path, session_id="sid",
    )

    db.add_action_item.assert_called_once()
    msg = db.add_action_item.call_args[1]["message"]
    assert "[RQ]" in msg, f"action item must start with [RQ]: {msg!r}"
    assert "Decide:" in msg, f"action item must include 'Decide:': {msg!r}"
    assert "Cause:" in msg, f"action item must state cause: {msg!r}"
    # Snapshot path must NOT be the headline (first 100 chars must not be a path dump)
    assert ".txt" not in msg[:100], f"snapshot path must not be the headline: {msg!r}"


def test_recovery_exhausted_action_item_not_emitted_as_notification(tmp_path):
    """retry_blocked → action item ONLY, not a passive notification as well."""
    from juggle_watchdog import execute_recovery

    agent = _dead_agent(watchdog_retried=1)
    db = _make_db_mock()
    db.get_agent.return_value = agent
    mgr = _make_mgr_mock(pane_exists=False)

    execute_recovery(
        db, mgr, agent, "pane content",
        recovery_dir=tmp_path, session_id="sid",
    )

    # The exhausted event is an action item, not a silent notification
    db.add_action_item.assert_called_once()


# ---------------------------------------------------------------------------
# 3. Orphan auto-recovery (below max attempts) → notification, not action item
# ---------------------------------------------------------------------------


def test_orphan_auto_recovery_emits_notification_not_action_item(tmp_path):
    """Orphan auto-recovery attempt (< max) → notification only."""
    from juggle_db import JuggleDB
    from juggle_watchdog import check_orphaned_threads

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()

    thread_id = db.create_thread("orphan test", session_id="")
    # Manually set background status + old last_active_at
    # P8 Task 3.1: reaper reads nodes; route the seed through update_thread so it
    # mirrors state='background' + last_active_at + last_dispatched_* onto the node.
    db.update_thread(
        thread_id, status="background", last_active_at="2020-01-01T00:00:00",
        last_dispatched_task="redo work", last_dispatched_role="coder",
    )

    mgr = MagicMock()
    new_agent = {"id": "c" * 32, "pane_id": "%10"}
    mgr.spawn_agent.return_value = new_agent

    with patch.object(db, "add_action_item") as mock_action, \
         patch.object(db, "add_notification_v2") as mock_notif:
        check_orphaned_threads(
            db,
            orphan_threshold=1.0,
            mgr=mgr,
            max_recovery_attempts=2,
        )

        # Auto-recovery succeeded → notification, not action item
        mock_notif.assert_called()
        mock_action.assert_not_called()


def test_orphan_auto_recovery_ignores_stale_dispatched_model(tmp_path):
    """Orphan auto-recovery must NOT forward a stale last_dispatched_model — the
    respawn has to re-resolve the launch model from current config (mirrors the
    'Fix 4' rule already applied in execute_recovery), not the snapshot value
    recorded at the ORIGINAL dispatch (2026-07-01 coder model config ignored)."""
    from juggle_db import JuggleDB
    from juggle_watchdog import check_orphaned_threads

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()

    thread_id = db.create_thread("orphan model test", session_id="")
    db.update_thread(
        thread_id, status="background", last_active_at="2020-01-01T00:00:00",
        last_dispatched_task="redo work", last_dispatched_role="coder",
        last_dispatched_model="opus",
    )

    mgr = MagicMock()
    new_agent = {"id": "c" * 32, "pane_id": "%10"}
    mgr.spawn_agent.return_value = new_agent

    check_orphaned_threads(
        db,
        orphan_threshold=1.0,
        mgr=mgr,
        max_recovery_attempts=2,
    )

    mgr.spawn_agent.assert_called_once()
    _, kwargs = mgr.spawn_agent.call_args
    assert kwargs.get("model") is None, (
        f"orphan recovery forwarded stale model {kwargs.get('model')!r} instead "
        "of letting spawn_agent re-resolve from current config"
    )


# ---------------------------------------------------------------------------
# 4. Orphan recovery exhausted (>= max attempts) → action item with [RQ] + Decide
# ---------------------------------------------------------------------------


def test_orphan_recovery_exhausted_emits_decision_action_item(tmp_path):
    """Orphan with max attempts already reached → action item with [RQ] + Decide."""
    from juggle_db import JuggleDB
    from juggle_watchdog import check_orphaned_threads

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()

    thread_id = db.create_thread("orphan-exhausted test", session_id="")
    # P8 Task 3.1: reaper reads nodes; route the seed through update_thread so it
    # mirrors state='background' + last_active_at + last_dispatched_* onto the node.
    db.update_thread(
        thread_id, status="background", last_active_at="2020-01-01T00:00:00",
        last_dispatched_task="redo work", last_dispatched_role="coder",
    )

    # Pre-populate watchdog_events to simulate max attempts already consumed
    max_attempts = 2
    for _ in range(max_attempts):
        db.add_watchdog_event(
            agent_id="orphan_detector",
            thread_id=thread_id,
            event_type="orphan_recovery",
            snapshot_path=None,
        )

    mgr = MagicMock()
    new_agent = {"id": "c" * 32, "pane_id": "%10"}
    mgr.spawn_agent.return_value = new_agent

    check_orphaned_threads(
        db,
        orphan_threshold=1.0,
        mgr=mgr,
        max_recovery_attempts=max_attempts,
    )

    items = db.get_open_action_items()
    assert items, "Expected at least one action item for exhausted orphan recovery"
    msg = items[0]["message"]
    assert "[RQ]" in msg, f"action item must contain [RQ]: {msg!r}"
    assert "Decide:" in msg, f"action item must include 'Decide:': {msg!r}"


# ---------------------------------------------------------------------------
# 5. Singleton — second start kills watchdog PIDs only
# ---------------------------------------------------------------------------


def test_kill_existing_watchdog_skips_non_watchdog_process(tmp_path, monkeypatch):
    """Must NOT kill a process whose cmdline does not contain 'watchdog'."""
    from juggle_watchdog import _kill_existing_watchdog_from_pidfile

    pidfile = tmp_path / "watchdog.pid"
    pidfile.write_text("99999")

    killed: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            return  # process "exists"
        killed.append((pid, sig))

    monkeypatch.setattr("os.kill", fake_kill)
    monkeypatch.setattr("juggle_watchdog._is_watchdog_process", lambda pid: False)

    _kill_existing_watchdog_from_pidfile(pidfile)

    assert killed == [], "Must not kill a process that is not a watchdog"


def test_kill_existing_watchdog_kills_confirmed_watchdog(tmp_path, monkeypatch):
    """Must send SIGTERM to a process whose cmdline confirms it is a watchdog."""
    from juggle_watchdog import _kill_existing_watchdog_from_pidfile

    pidfile = tmp_path / "watchdog.pid"
    pidfile.write_text("99999")

    killed: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if killed:
                # Process died after SIGTERM — next existence probe raises
                raise ProcessLookupError
            return  # alive initially
        killed.append((pid, sig))

    monkeypatch.setattr("os.kill", fake_kill)
    monkeypatch.setattr("juggle_watchdog._is_watchdog_process", lambda pid: True)

    _kill_existing_watchdog_from_pidfile(pidfile)

    assert any(sig == signal.SIGTERM for _, sig in killed), (
        "Must send SIGTERM to confirmed watchdog PID"
    )


def test_kill_existing_watchdog_skips_own_pid(tmp_path, monkeypatch):
    """Must not kill itself even if pidfile contains our own PID."""
    import os
    from juggle_watchdog import _kill_existing_watchdog_from_pidfile

    pidfile = tmp_path / "watchdog.pid"
    pidfile.write_text(str(os.getpid()))

    killed: list = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)) if sig != 0 else None)
    monkeypatch.setattr("juggle_watchdog._is_watchdog_process", lambda pid: True)

    _kill_existing_watchdog_from_pidfile(pidfile)

    assert not any(sig != 0 for _, sig in killed), "Must not kill own PID"


def test_kill_existing_watchdog_handles_missing_pidfile(tmp_path):
    """Must handle gracefully when pidfile does not exist."""
    from juggle_watchdog import _kill_existing_watchdog_from_pidfile

    pidfile = tmp_path / "watchdog.pid"
    # No write — file does not exist

    # Should not raise
    _kill_existing_watchdog_from_pidfile(pidfile)
