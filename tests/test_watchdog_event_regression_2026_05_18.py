"""Regression tests for watchdog events from 2026-05-18.

Focused regression tests for:
- retry_blocked: agent stalls again after watchdog retry (watchdog_retried >= 1)
- stalled: agent stalls with no task content to replay (last_task is None/empty)

These tests verify event detection, snapshot creation, and action item escalation.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB
from juggle_watchdog import execute_recovery


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


@pytest.fixture
def mock_mgr():
    mgr = MagicMock()
    mgr.kill_pane = MagicMock()
    mgr.spawn_agent = MagicMock(
        return_value={
            "id": "new-agent-id",
            "pane_id": "%99",
            "status": "busy",
        }
    )
    mgr.send_task = MagicMock()
    return mgr


# =============================================================================
# retry_blocked Event Tests (3 regression cases from 2026-05-18)
# =============================================================================


def test_retry_blocked_event_created_when_watchdog_retried_1(db, mock_mgr, tmp_path):
    """Regression: retry_blocked event created when agent stalls after first retry.

    Event ID: f1a4bcf6-89ef-4103-863d-cf725e4ddf7e (2026-05-18 08:00:51)

    Scenario: watchdog_retried=1 indicates agent has already been retried once.
    Second stall must be blocked (no further retries) to prevent infinite loops.
    """
    thread_id = db.create_thread("retry blocked case 1", session_id="test-session")
    agent_id = db.create_agent(role="coder", pane_id="%10")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="implement feature",
        watchdog_retried=1,
    )

    agent = db.get_agent(agent_id)
    pane_content = "Stalled after watchdog retry"
    recovery_dir = tmp_path / "recovery"

    execute_recovery(
        db,
        mock_mgr,
        agent,
        pane_content,
        recovery_dir=recovery_dir,
        session_id="test-session",
    )

    # Must NOT spawn new agent
    mock_mgr.spawn_agent.assert_not_called()

    # Must create retry_blocked event
    with db._connect() as conn:
        events = conn.execute(
            "SELECT * FROM watchdog_events WHERE agent_id=? ORDER BY created_at",
            (agent_id,),
        ).fetchall()
    assert len(events) == 1
    assert events[0]["event_type"] == "retry_blocked"

    # Must create high-priority action item
    with db._connect() as conn:
        items = conn.execute(
            "SELECT * FROM action_items WHERE thread_id=? ORDER BY created_at DESC",
            (thread_id,),
        ).fetchall()
    assert len(items) > 0
    assert items[0]["priority"] == "high"
    assert "AGAIN after watchdog retry" in items[0]["message"]


def test_retry_blocked_event_multiple_failures(db, mock_mgr, tmp_path):
    """Regression: retry_blocked also triggered when watchdog_retried > 1.

    Edge case: agent has failed multiple retries (watchdog_retried=2+).
    Verify blocking still applies regardless of retry count.
    """
    thread_id = db.create_thread("retry blocked case 2", session_id="test-session")
    agent_id = db.create_agent(role="planner", pane_id="%20")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="plan the feature",
        watchdog_retried=2,
    )

    agent = db.get_agent(agent_id)
    recovery_dir = tmp_path / "recovery"

    execute_recovery(
        db,
        mock_mgr,
        agent,
        "stalled multiple times",
        recovery_dir=recovery_dir,
        session_id="test-session",
    )

    mock_mgr.spawn_agent.assert_not_called()

    with db._connect() as conn:
        events = conn.execute(
            "SELECT event_type FROM watchdog_events WHERE agent_id=?", (agent_id,)
        ).fetchall()
    assert len(events) == 1
    assert events[0][0] == "retry_blocked"


def test_retry_blocked_snapshot_preserved(db, mock_mgr, tmp_path):
    """Regression: retry_blocked snapshot must be saved before blocking retry.

    Snapshot is critical for debugging why the retry failed.
    Verify snapshot path is stored and file exists with correct content.
    """
    thread_id = db.create_thread("retry blocked snapshot", session_id="test-session")
    agent_id = db.create_agent(role="researcher", pane_id="%30")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="research the topic",
        watchdog_retried=1,
    )

    agent = db.get_agent(agent_id)
    debug_output = "Failed output trace\nError in recovery attempt\nStack trace here"
    recovery_dir = tmp_path / "recovery"

    execute_recovery(
        db,
        mock_mgr,
        agent,
        debug_output,
        recovery_dir=recovery_dir,
        session_id="test-session",
    )

    with db._connect() as conn:
        events = conn.execute(
            "SELECT snapshot_path FROM watchdog_events WHERE agent_id=? AND event_type=?",
            (agent_id, "retry_blocked"),
        ).fetchall()

    assert len(events) == 1
    snap_path = Path(events[0][0])
    assert snap_path.exists(), f"Snapshot not found at {snap_path}"

    snap_content = snap_path.read_text()
    assert debug_output in snap_content, "Debug output not in snapshot"


# =============================================================================
# stalled Event Tests (1 regression case from 2026-05-18)
# =============================================================================


def test_stalled_event_no_task_content(db, mock_mgr, tmp_path):
    """Regression: agent with last_task=None is silently decommissioned (not 'stalled').

    Event ID: e86a33f5-4ec6-46a8-8129-e5b5021e9547 (2026-05-18 05:25:09)

    OLD (buggy) behaviour: created a high-priority action item + marked thread failed.
    NEW (correct) behaviour: silently decommissioned — no action item, thread not failed,
    event_type='decommissioned_untasked'.
    """
    thread_id = db.create_thread("no task stall", session_id="test-session")
    agent_id = db.create_agent(role="coder", pane_id="%40")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task=None,
        watchdog_retried=0,
    )

    agent = db.get_agent(agent_id)
    pane_content = "Agent frozen, no recovery possible"
    recovery_dir = tmp_path / "recovery"

    execute_recovery(
        db,
        mock_mgr,
        agent,
        pane_content,
        recovery_dir=recovery_dir,
        session_id="test-session",
    )

    # Must NOT spawn new agent
    mock_mgr.spawn_agent.assert_not_called()

    # Must create decommissioned_untasked event (NOT 'stalled')
    with db._connect() as conn:
        events = conn.execute(
            "SELECT * FROM watchdog_events WHERE agent_id=?", (agent_id,)
        ).fetchall()
    assert len(events) == 1
    assert events[0]["event_type"] == "decommissioned_untasked"

    # Must NOT create any action items (was a false alert)
    with db._connect() as conn:
        items = conn.execute(
            "SELECT * FROM action_items WHERE thread_id=?", (thread_id,)
        ).fetchall()
    assert len(items) == 0

    # Original agent must be deleted
    assert db.get_agent(agent_id) is None

    # Thread must NOT be marked failed
    thread = db.get_thread(thread_id)
    assert thread["status"] != "failed"


def test_stalled_event_empty_task_string(db, mock_mgr, tmp_path):
    """Empty task string treated same as no task — silently decommissioned."""
    thread_id = db.create_thread("empty task stall", session_id="test-session")
    agent_id = db.create_agent(role="coder", pane_id="%50")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="",
        watchdog_retried=0,
    )

    agent = db.get_agent(agent_id)
    recovery_dir = tmp_path / "recovery"

    execute_recovery(
        db,
        mock_mgr,
        agent,
        "frozen",
        recovery_dir=recovery_dir,
        session_id="test-session",
    )

    mock_mgr.spawn_agent.assert_not_called()

    with db._connect() as conn:
        events = conn.execute(
            "SELECT event_type FROM watchdog_events WHERE agent_id=?", (agent_id,)
        ).fetchall()
    assert len(events) == 1
    assert events[0][0] == "decommissioned_untasked"


def test_stalled_event_snapshot_preserved(db, mock_mgr, tmp_path):
    """Decommissioned_untasked event has no snapshot (not needed — no task was sent).

    The decommission path is a clean teardown, not a failure debug snapshot.
    """
    thread_id = db.create_thread("stalled snapshot", session_id="test-session")
    agent_id = db.create_agent(role="researcher", pane_id="%60")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task=None,
        watchdog_retried=0,
    )

    agent = db.get_agent(agent_id)
    pane_output = (
        "Last output before freeze:\nWaiting for user input?\nNo visible progress"
    )
    recovery_dir = tmp_path / "recovery"

    execute_recovery(
        db,
        mock_mgr,
        agent,
        pane_output,
        recovery_dir=recovery_dir,
        session_id="test-session",
    )

    with db._connect() as conn:
        events = conn.execute(
            "SELECT snapshot_path FROM watchdog_events WHERE agent_id=? AND event_type=?",
            (agent_id, "decommissioned_untasked"),
        ).fetchall()

    assert len(events) == 1
    # snapshot_path is None for the decommission path
    assert events[0][0] is None


# =============================================================================
# State Transitions & Cleanup
# =============================================================================


def test_retry_blocked_deletes_original_agent(db, mock_mgr, tmp_path):
    """Retry_blocked must delete original agent from DB."""
    agent_id = db.create_agent(role="coder", pane_id="%70")
    thread_id = db.create_thread("delete agent", session_id="test-session")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="task",
        watchdog_retried=1,
    )

    agent = db.get_agent(agent_id)
    assert agent is not None

    execute_recovery(
        db,
        mock_mgr,
        agent,
        "stalled",
        recovery_dir=tmp_path / "recovery",
        session_id="test-session",
    )

    assert db.get_agent(agent_id) is None


def test_stalled_deletes_original_agent(db, mock_mgr, tmp_path):
    """Stalled must delete original agent from DB."""
    agent_id = db.create_agent(role="researcher", pane_id="%80")
    thread_id = db.create_thread("delete stalled", session_id="test-session")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task=None,
        watchdog_retried=0,
    )

    agent = db.get_agent(agent_id)
    assert agent is not None

    execute_recovery(
        db,
        mock_mgr,
        agent,
        "frozen",
        recovery_dir=tmp_path / "recovery",
        session_id="test-session",
    )

    assert db.get_agent(agent_id) is None


def test_retry_blocked_kills_original_pane(db, mock_mgr, tmp_path):
    """Retry_blocked must attempt to kill original pane."""
    agent_id = db.create_agent(role="coder", pane_id="%100")
    thread_id = db.create_thread("kill pane", session_id="test-session")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="task",
        watchdog_retried=1,
    )

    agent = db.get_agent(agent_id)

    execute_recovery(
        db,
        mock_mgr,
        agent,
        "stalled",
        recovery_dir=tmp_path / "recovery",
        session_id="test-session",
    )

    mock_mgr.kill_pane.assert_called_once_with("%100")


def test_stalled_kills_original_pane(db, mock_mgr, tmp_path):
    """Stalled must attempt to kill original pane."""
    agent_id = db.create_agent(role="researcher", pane_id="%110")
    thread_id = db.create_thread("kill stalled pane", session_id="test-session")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task=None,
        watchdog_retried=0,
    )

    agent = db.get_agent(agent_id)

    execute_recovery(
        db,
        mock_mgr,
        agent,
        "frozen",
        recovery_dir=tmp_path / "recovery",
        session_id="test-session",
    )

    mock_mgr.kill_pane.assert_called_once_with("%110")


def test_retry_blocked_thread_marked_failed(db, mock_mgr, tmp_path):
    """Retry_blocked must mark thread as failed (no retry attempted)."""
    thread_id = db.create_thread("thread failed", session_id="test-session")
    agent_id = db.create_agent(role="coder", pane_id="%120")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="task",
        watchdog_retried=1,
    )

    agent = db.get_agent(agent_id)

    execute_recovery(
        db,
        mock_mgr,
        agent,
        "stalled",
        recovery_dir=tmp_path / "recovery",
        session_id="test-session",
    )

    thread = db.get_thread(thread_id)
    assert thread["status"] == "failed"


def test_stalled_thread_marked_failed(db, mock_mgr, tmp_path):
    """Decommissioned_untasked must NOT mark thread as failed (was never tasked)."""
    thread_id = db.create_thread("stalled thread failed", session_id="test-session")
    agent_id = db.create_agent(role="coder", pane_id="%130")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task=None,
        watchdog_retried=0,
    )

    agent = db.get_agent(agent_id)

    execute_recovery(
        db,
        mock_mgr,
        agent,
        "frozen",
        recovery_dir=tmp_path / "recovery",
        session_id="test-session",
    )

    thread = db.get_thread(thread_id)
    assert thread["status"] != "failed"


# =============================================================================
# Event Sequence & Timestamps
# =============================================================================


def test_retry_blocked_event_has_correct_fields(db, mock_mgr, tmp_path):
    """Retry_blocked event record has all required fields."""
    thread_id = db.create_thread("event fields", session_id="test-session")
    agent_id = db.create_agent(role="coder", pane_id="%140")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="task",
        watchdog_retried=1,
    )

    agent = db.get_agent(agent_id)

    execute_recovery(
        db,
        mock_mgr,
        agent,
        "stalled",
        recovery_dir=tmp_path / "recovery",
        session_id="test-session",
    )

    with db._connect() as conn:
        events = conn.execute(
            "SELECT agent_id, thread_id, event_type, snapshot_path, created_at "
            "FROM watchdog_events WHERE agent_id=?",
            (agent_id,),
        ).fetchall()

    assert len(events) == 1
    evt = events[0]
    assert evt[0] == agent_id
    assert evt[1] == thread_id
    assert evt[2] == "retry_blocked"
    assert evt[3] is not None
    assert evt[4] is not None


def test_stalled_event_has_correct_fields(db, mock_mgr, tmp_path):
    """Decommissioned_untasked event record has all required fields (no snapshot_path)."""
    thread_id = db.create_thread("stalled fields", session_id="test-session")
    agent_id = db.create_agent(role="researcher", pane_id="%150")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task=None,
        watchdog_retried=0,
    )

    agent = db.get_agent(agent_id)

    execute_recovery(
        db,
        mock_mgr,
        agent,
        "frozen",
        recovery_dir=tmp_path / "recovery",
        session_id="test-session",
    )

    with db._connect() as conn:
        events = conn.execute(
            "SELECT agent_id, thread_id, event_type, snapshot_path, created_at "
            "FROM watchdog_events WHERE agent_id=?",
            (agent_id,),
        ).fetchall()

    assert len(events) == 1
    evt = events[0]
    assert evt[0] == agent_id
    assert evt[1] == thread_id
    assert evt[2] == "decommissioned_untasked"
    assert evt[3] is None   # no snapshot for silent decommission
    assert evt[4] is not None
