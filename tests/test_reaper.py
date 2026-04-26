"""Tests for stale-agent reaping at 12h TTL."""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_reaper_removes_agent_after_12h_idle():
    """Reaper should remove agents idle > 12h."""
    from juggle_tmux import reap_stale_agents

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()

    old_agent = {
        "id": "old-agent-id",
        "status": "idle",
        "assigned_thread": "other-thread",
        "pane_id": "pane-123",
        "last_active": (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat(),
    }

    mock_db.get_all_agents.return_value = [old_agent]
    mock_db.get_current_thread.return_value = "current-thread"
    mock_mgr.verify_pane.return_value = True

    reaped = reap_stale_agents(mock_db, mock_mgr)

    assert reaped == 1, f"Expected 1 agent reaped, got {reaped}"
    mock_mgr.decommission_agent.assert_called_once_with(mock_db, "old-agent-id")


def test_reaper_preserves_recent_agents():
    """Reaper should not remove agents idle < 12h."""
    from juggle_tmux import reap_stale_agents

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()

    recent_agent = {
        "id": "recent-agent",
        "status": "idle",
        "assigned_thread": "other-thread",
        "pane_id": "pane-456",
        "last_active": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    }

    mock_db.get_all_agents.return_value = [recent_agent]
    mock_db.get_current_thread.return_value = "current-thread"
    mock_mgr.verify_pane.return_value = True

    reaped = reap_stale_agents(mock_db, mock_mgr)

    assert reaped == 0, f"Expected 0 agents reaped, got {reaped}"
    mock_mgr.decommission_agent.assert_not_called()


def test_reaper_skips_busy_agents_with_live_pane():
    """Reaper should not reap busy agents whose pane still exists."""
    from juggle_tmux import reap_stale_agents

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()

    busy_agent = {
        "id": "busy-agent",
        "status": "busy",
        "assigned_thread": "other-thread",
        "pane_id": "pane-789",
        "last_active": (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat(),
    }

    mock_db.get_all_agents.return_value = [busy_agent]
    mock_db.get_current_thread.return_value = "current-thread"
    mock_mgr.verify_pane.return_value = True

    reaped = reap_stale_agents(mock_db, mock_mgr)

    assert reaped == 0
    mock_mgr.decommission_agent.assert_not_called()
    mock_db.delete_agent.assert_not_called()


def test_reaper_removes_busy_agent_with_dead_pane():
    """Busy agent whose pane no longer exists should be reaped immediately."""
    from juggle_tmux import reap_stale_agents

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()

    zombie_agent = {
        "id": "zombie-agent",
        "status": "busy",
        "assigned_thread": "other-thread",
        "pane_id": "pane-dead-busy",
        "last_active": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    }

    mock_db.get_all_agents.return_value = [zombie_agent]
    mock_db.get_current_thread.return_value = "current-thread"
    mock_mgr.verify_pane.return_value = False

    reaped = reap_stale_agents(mock_db, mock_mgr)

    assert reaped == 1
    mock_db.delete_agent.assert_called_once_with("zombie-agent")
    mock_mgr.decommission_agent.assert_not_called()


def test_reaper_skips_current_thread_agent():
    """Reaper should never reap agent assigned to current thread."""
    from juggle_tmux import reap_stale_agents

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()

    current_agent = {
        "id": "current-agent",
        "status": "idle",
        "assigned_thread": "current-thread",
        "pane_id": "pane-999",
        "last_active": (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat(),
    }

    mock_db.get_all_agents.return_value = [current_agent]
    mock_db.get_current_thread.return_value = "current-thread"
    mock_mgr.verify_pane.return_value = True

    reaped = reap_stale_agents(mock_db, mock_mgr)

    assert reaped == 0
    mock_mgr.decommission_agent.assert_not_called()


def test_reaper_removes_dead_pane_agent():
    """Reaper should call db.delete_agent directly when pane no longer exists."""
    from juggle_tmux import reap_stale_agents

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()

    dead_pane_agent = {
        "id": "dead-pane-agent",
        "status": "idle",
        "assigned_thread": "other-thread",
        "pane_id": "pane-dead",
        "last_active": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    }

    mock_db.get_all_agents.return_value = [dead_pane_agent]
    mock_db.get_current_thread.return_value = "current-thread"
    mock_mgr.verify_pane.return_value = False

    reaped = reap_stale_agents(mock_db, mock_mgr)

    assert reaped == 1
    mock_db.delete_agent.assert_called_once_with("dead-pane-agent")
    mock_mgr.decommission_agent.assert_not_called()
