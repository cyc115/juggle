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


def test_reaper_reaps_decommission_pending_agent():
    """decommission_pending agents with live panes must be reaped immediately."""
    from juggle_tmux import reap_stale_agents

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()

    pending_agent = {
        "id": "pending-agent",
        "status": "decommission_pending",
        "assigned_thread": "other-thread",
        "pane_id": "pane-pending",
        "last_active": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    }

    mock_db.get_all_agents.return_value = [pending_agent]
    mock_db.get_current_thread.return_value = "current-thread"
    mock_mgr.verify_pane.return_value = True

    reaped = reap_stale_agents(mock_db, mock_mgr)

    assert reaped == 1
    mock_mgr.decommission_agent.assert_called_once_with(mock_db, "pending-agent")


import time as _time_mod


def test_pass2_skips_orphan_pane_within_boot_grace():
    """Pass 2 must NOT kill a JUGGLE_IS_AGENT pane younger than boot grace."""
    from juggle_tmux import reap_stale_agents

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()
    mock_mgr.session_name = "juggle"
    mock_db.get_all_agents.return_value = []
    mock_db.get_current_thread.return_value = "t1"

    fresh_start = int(_time_mod.time())  # just created

    def fake_run(cmd, **kwargs):
        r = mock.MagicMock()
        if "list-panes" in cmd:
            r.returncode = 0
            r.stdout = "%pane-new\n"
        elif "display-message" in cmd:
            r.returncode = 0
            r.stdout = str(fresh_start)
        else:
            r.returncode = 1
            r.stdout = ""
        return r

    with mock.patch("subprocess.run", side_effect=fake_run):
        with mock.patch("juggle_tmux._pane_has_juggle_agent_env", return_value=True):
            reaped = reap_stale_agents(mock_db, mock_mgr)

    assert reaped == 0, f"should not reap pane within grace, got {reaped}"
    mock_mgr.kill_pane.assert_not_called()


def test_pass2_kills_orphan_pane_past_boot_grace():
    """Pass 2 MUST kill a JUGGLE_IS_AGENT pane older than boot grace with no DB record."""
    from juggle_tmux import reap_stale_agents

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()
    mock_mgr.session_name = "juggle"
    mock_db.get_all_agents.return_value = []
    mock_db.get_current_thread.return_value = "t1"

    old_start = int(_time_mod.time()) - 300  # 5 min old, well past 120s grace

    def fake_run(cmd, **kwargs):
        r = mock.MagicMock()
        if "list-panes" in cmd:
            r.returncode = 0
            r.stdout = "%pane-old\n"
        elif "display-message" in cmd:
            r.returncode = 0
            r.stdout = str(old_start)
        else:
            r.returncode = 1
            r.stdout = ""
        return r

    with mock.patch("subprocess.run", side_effect=fake_run):
        with mock.patch("juggle_tmux._pane_has_juggle_agent_env", return_value=True):
            reaped = reap_stale_agents(mock_db, mock_mgr)

    assert reaped == 1, f"should reap old orphan pane, got {reaped}"
    mock_mgr.kill_pane.assert_called_once_with("%pane-old")


def test_pass2_skips_pane_when_start_time_unreadable():
    """Pass 2 is conservative: if pane age can't be read, skip (don't kill)."""
    from juggle_tmux import reap_stale_agents

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()
    mock_mgr.session_name = "juggle"
    mock_db.get_all_agents.return_value = []
    mock_db.get_current_thread.return_value = "t1"

    def fake_run(cmd, **kwargs):
        r = mock.MagicMock()
        if "list-panes" in cmd:
            r.returncode = 0
            r.stdout = "%pane-unknown\n"
        elif "display-message" in cmd:
            r.returncode = 1  # tmux can't read start time
            r.stdout = ""
        else:
            r.returncode = 1
            r.stdout = ""
        return r

    with mock.patch("subprocess.run", side_effect=fake_run):
        with mock.patch("juggle_tmux._pane_has_juggle_agent_env", return_value=True):
            reaped = reap_stale_agents(mock_db, mock_mgr)

    assert reaped == 0, "should not kill when pane age is unreadable"
    mock_mgr.kill_pane.assert_not_called()
