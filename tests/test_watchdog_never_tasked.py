"""TDD tests for watchdog never-tasked agent fix.

Covers:
  1. classify_pane_state returns 'awaiting_dispatch' (not 'stalled') when
     last_send_task_at=None even past threshold.
  2. The recovery entry path SKIPS recovery for an awaiting_dispatch /
     never-tasked agent (no action item created, thread not marked failed).
  3. execute_recovery with no last_task creates NO action item and does NOT
     set thread status='failed' — it silently decommissions.
  4. Existing behaviour preserved: agent that HAD a task and is unresponsive
     still triggers the normal stalled/recovery path.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure src/ on path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_watchdog import classify_pane_state, execute_recovery
from juggle_db import JuggleDB


# ---------------------------------------------------------------------------
# DB fixture (in-memory, same pattern as other watchdog tests)
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    os.environ["_JUGGLE_TEST_DB"] = db_path
    os.environ["CLAUDE_PLUGIN_DATA"] = str(tmp_path)
    os.environ["JUGGLE_MAX_BACKGROUND_AGENTS"] = "5"
    os.environ["JUGGLE_MAX_THREADS"] = "10"
    instance = JuggleDB(db_path)
    instance.init_db()
    yield instance
    del os.environ["_JUGGLE_TEST_DB"]


@pytest.fixture
def mock_mgr():
    mgr = MagicMock()
    mgr.verify_pane.return_value = False   # pane dead → don't take alive_slow path
    mgr.spawn_agent.return_value = {
        "id": "new-agent-id",
        "pane_id": "%99",
    }
    return mgr


# ---------------------------------------------------------------------------
# 1. classify_pane_state: last_send_task_at=None → awaiting_dispatch
# ---------------------------------------------------------------------------


def test_classify_never_dispatched_returns_awaiting_dispatch():
    """When last_send_task_at=None agent has never been tasked.
    Even if content is stale, classify should return awaiting_dispatch, not stalled."""
    state, key = classify_pane_state(
        content="Claude is ready\nwaiting...",
        prev_content="Claude is ready\nwaiting...",  # unchanged → would normally be stalled
        stalled_for=999.0,   # well past any threshold
        threshold=300.0,
        last_send_task_at=None,  # never dispatched
    )
    assert state == "awaiting_dispatch", (
        f"Expected 'awaiting_dispatch', got {state!r}. "
        "Agents that were never tasked must not be classified as stalled."
    )


def test_classify_with_task_and_past_threshold_returns_stalled():
    """Existing behaviour preserved: agent that WAS dispatched and is now stale
    past threshold should still return 'stalled'."""
    state, key = classify_pane_state(
        content="Claude is ready\nwaiting...",
        prev_content="Claude is ready\nwaiting...",
        stalled_for=999.0,
        threshold=300.0,
        last_send_task_at="2026-01-01T00:00:00Z",  # was dispatched
    )
    assert state == "stalled"


# ---------------------------------------------------------------------------
# 2. execute_recovery: no last_task → silently decommission, no action item,
#    no thread=failed
# ---------------------------------------------------------------------------


def test_execute_recovery_no_last_task_no_action_item(db, mock_mgr, tmp_path):
    """execute_recovery with last_task=None must NOT create an action item.

    Agents that were spawned but never sent a task are not truly stalled —
    filing a high-priority manual-intervention action item is a false alert.
    """
    thread_id = db.create_thread("never tasked thread", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%10")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task=None,
        watchdog_retried=0,
    )

    agent = db.get_agent(agent_id)
    recovery_dir = tmp_path / "recovery"

    execute_recovery(
        db, mock_mgr, agent, "idle output",
        recovery_dir=recovery_dir, session_id="test",
    )

    # No action items must have been created
    items = db.get_open_action_items()
    assert items == [], (
        f"Expected no action items, got {items}. "
        "Never-tasked agents should be silently decommissioned."
    )


def test_execute_recovery_no_last_task_thread_not_failed(db, mock_mgr, tmp_path):
    """execute_recovery with last_task=None must NOT mark thread as failed."""
    thread_id = db.create_thread("never tasked thread 2", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%11")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task=None,
        watchdog_retried=0,
    )

    agent = db.get_agent(agent_id)
    recovery_dir = tmp_path / "recovery"

    execute_recovery(
        db, mock_mgr, agent, "idle output",
        recovery_dir=recovery_dir, session_id="test",
    )

    thread = db.get_thread(thread_id)
    assert thread["status"] != "failed", (
        f"Thread should not be 'failed', got {thread['status']!r}. "
        "Never-tasked agents must not fail the thread."
    )


def test_execute_recovery_no_last_task_agent_decommissioned(db, mock_mgr, tmp_path):
    """execute_recovery with last_task=None decommissions the agent (pane killed,
    agent deleted from DB) with a 'decommissioned_untasked' watchdog event."""
    thread_id = db.create_thread("decommission thread", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%12")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task=None,
        watchdog_retried=0,
    )

    agent = db.get_agent(agent_id)
    pane_id = agent["pane_id"]
    recovery_dir = tmp_path / "recovery"

    execute_recovery(
        db, mock_mgr, agent, "idle",
        recovery_dir=recovery_dir, session_id="test",
    )

    # Pane must have been killed
    mock_mgr.kill_pane.assert_called_once_with(pane_id)

    # Agent must be deleted from DB
    assert db.get_agent(agent_id) is None, "Agent should be deleted after decommission"

    # Watchdog event of type decommissioned_untasked
    events = db.get_watchdog_events(agent_id)
    assert any(e["event_type"] == "decommissioned_untasked" for e in events), (
        f"Expected 'decommissioned_untasked' event, got {[e['event_type'] for e in events]}"
    )


# ---------------------------------------------------------------------------
# 4. Regression: agent with last_task still goes through normal recovery
# ---------------------------------------------------------------------------


def test_execute_recovery_with_last_task_creates_action_item(db, mock_mgr, tmp_path):
    """Agent that had a real task and stalled should still create action item and retry."""
    thread_id = db.create_thread("has task thread", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%20")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="do some work",
        watchdog_retried=0,
    )

    agent = db.get_agent(agent_id)
    recovery_dir = tmp_path / "recovery"

    execute_recovery(
        db, mock_mgr, agent, "frozen output",
        recovery_dir=recovery_dir, session_id="test",
    )

    # Should have spawned a new agent (retry path)
    mock_mgr.spawn_agent.assert_called_once()

    # Should have created an action item (stalled/crashed notification)
    items = db.get_open_action_items()
    assert len(items) > 0, "Agent with last_task should create action item on recovery"
