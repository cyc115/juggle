"""Tests for watchdog policy: alive_slow vs dead vs never_fired classification,
nudge_and_notify behavior, and execute_recovery short-circuit."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# _classify_agent_state
# ---------------------------------------------------------------------------


def test_classify_pane_gone_is_dead():
    from juggle_watchdog import _classify_agent_state

    assert _classify_agent_state("", pane_exists=False) == "dead"


def test_classify_claude_ui_marker_is_alive_slow():
    from juggle_watchdog import _classify_agent_state

    for marker in (
        "Welcome",
        "Bypass permissions",
        "INSERT",
        "Cogitated",
        "Working",
        "shortcuts",
        "claude.ai/code",
    ):
        content = f"some output\n{marker}\nmore lines"
        assert _classify_agent_state(content, pane_exists=True) == "alive_slow", (
            f"Expected alive_slow for marker {marker!r}"
        )


def test_classify_shell_prompt_no_claude_ui_is_never_fired():
    from juggle_watchdog import _classify_agent_state

    content = "Last login: Mon May 19 12:00:00\nmikechen@host:~$ "
    assert _classify_agent_state(content, pane_exists=True) == "never_fired"


def test_classify_empty_pane_existing_is_never_fired():
    from juggle_watchdog import _classify_agent_state

    assert _classify_agent_state("", pane_exists=True) == "never_fired"


# ---------------------------------------------------------------------------
# nudge_and_notify — must NOT call kill_pane
# ---------------------------------------------------------------------------


def _make_db_mock(thread_id="thread-abc"):
    db = MagicMock()
    db.get_thread.return_value = {"user_label": "test-thread", "label": "test-thread"}
    return db


def _make_agent(pane_id="%42", thread_id="thread-abc"):
    return {
        "id": "a" * 32,
        "pane_id": pane_id,
        "assigned_thread": thread_id,
        "role": "coder",
        "last_active": None,
    }


def test_nudge_and_notify_does_not_call_kill_pane():
    from juggle_watchdog import nudge_and_notify

    db = _make_db_mock()
    mgr = MagicMock()

    nudge_and_notify(db, mgr, _make_agent(), content="Cogitated for 3 min\nsome output")

    mgr.kill_pane.assert_not_called()


def test_nudge_and_notify_sends_enter():
    from juggle_watchdog import nudge_and_notify

    db = _make_db_mock()
    mgr = MagicMock()

    nudge_and_notify(db, mgr, _make_agent(pane_id="%99"), content="Working…")

    mgr._run_tmux.assert_called_once_with("send-keys", "-t", "%99", "Enter")


def test_nudge_and_notify_sends_notification_not_action_item():
    """alive-but-slow is informational — must send a notification, NOT file an action item."""
    from juggle_watchdog import nudge_and_notify

    db = _make_db_mock()
    mgr = MagicMock()

    nudge_and_notify(db, mgr, _make_agent(), content="Cogitated\nline2\nline3")

    db.add_notification_v2.assert_called_once()
    notif_kwargs = db.add_notification_v2.call_args[1]
    assert "alive-but-stalled" in notif_kwargs["message"]
    # Must NOT file a blocking action item
    db.add_action_item.assert_not_called()


# ---------------------------------------------------------------------------
# execute_recovery — short-circuits for alive_slow (no snapshot, no spawn, no kill)
# ---------------------------------------------------------------------------


def _make_live_agent(pane_id="%7"):
    return {
        "id": "b" * 32,
        "pane_id": pane_id,
        "status": "busy",
        "assigned_thread": "thread-xyz",
        "role": "coder",
        "model": None,
        "last_task": "do stuff",
        "watchdog_retried": 0,
        "last_active": None,
    }


def test_execute_recovery_short_circuits_for_alive_slow(tmp_path):
    from juggle_watchdog import execute_recovery

    db = MagicMock()
    db.get_agent.return_value = _make_live_agent()
    db.get_thread.return_value = {"user_label": "lbl", "label": "lbl"}
    mgr = MagicMock()
    mgr.verify_pane.return_value = True  # pane exists

    pane_content = "Cogitated for 5 minutes\nsome deep thinking"

    execute_recovery(
        db,
        mgr,
        _make_live_agent(),
        pane_content,
        recovery_dir=tmp_path,
        session_id="sess",
    )

    # Must not kill or spawn
    mgr.kill_pane.assert_not_called()
    mgr.spawn_agent.assert_not_called()
    # Must not write a recovery snapshot
    assert list(tmp_path.glob("*.txt")) == []
    # Must send a notification (via nudge_and_notify) — not a blocking action item
    db.add_notification_v2.assert_called_once()
    db.add_action_item.assert_not_called()


def test_execute_recovery_proceeds_for_dead_pane(tmp_path):
    from juggle_watchdog import execute_recovery

    db = MagicMock()
    db.get_agent.return_value = _make_live_agent()
    db.get_thread.return_value = {"user_label": "lbl", "label": "lbl"}
    db.get_median_duration_secs.return_value = None
    db.delete_agent.return_value = None
    new_agent = {"id": "c" * 32, "pane_id": "%8"}
    mgr = MagicMock()
    mgr.verify_pane.return_value = False  # pane gone
    mgr.spawn_agent.return_value = new_agent

    execute_recovery(
        db,
        mgr,
        _make_live_agent(),
        "",
        recovery_dir=tmp_path,
        session_id="sess",
    )

    # Should kill and spawn for dead pane
    mgr.kill_pane.assert_called_once()
    mgr.spawn_agent.assert_called_once()


# ---------------------------------------------------------------------------
# Issue #2 — cold-start failure rollback
# ---------------------------------------------------------------------------


def test_execute_recovery_cold_start_failure_rolls_back_thread(tmp_path):
    """send_task raising must roll thread back to failed, delete recovery agent, kill pane."""
    from juggle_watchdog import execute_recovery

    new_agent = {"id": "c" * 32, "pane_id": "%8"}
    db = MagicMock()
    db.get_agent.return_value = _make_live_agent()
    db.get_thread.return_value = {"user_label": "lbl", "label": "lbl"}
    mgr = MagicMock()
    mgr.verify_pane.return_value = False  # dead pane → proceed to recovery
    mgr.spawn_agent.return_value = new_agent
    mgr.send_task.side_effect = RuntimeError("Claude UI not ready in pane %8 after 30s")

    execute_recovery(
        db,
        mgr,
        _make_live_agent(),
        "",
        recovery_dir=tmp_path,
        session_id="sess",
    )

    # Thread must be rolled back to failed (not left in background)
    db.update_thread.assert_any_call("thread-xyz", status="failed")
    # Recovery agent must be decommissioned
    db.delete_agent.assert_any_call("c" * 32)
    # Recovery pane must be killed
    mgr.kill_pane.assert_any_call("%8")
    # Action item must be filed
    db.add_action_item.assert_called()
    call_kwargs = db.add_action_item.call_args[1]
    assert call_kwargs["priority"] == "high"
    assert "RECOVERY-COLD-START-FAILED" in call_kwargs["message"]


# ---------------------------------------------------------------------------
# Issue #3 — retry_blocked files high-priority action item
# ---------------------------------------------------------------------------


def test_execute_recovery_retry_blocked_files_high_priority_action_item(tmp_path):
    """retry_blocked (watchdog_retried>=1) must file a high-priority action item."""
    from juggle_watchdog import execute_recovery

    live = _make_live_agent()
    live["watchdog_retried"] = 1  # 2nd attempt — triggers retry_blocked
    db = MagicMock()
    db.get_agent.return_value = live
    db.get_thread.return_value = {"user_label": "lbl", "label": "lbl"}
    mgr = MagicMock()
    mgr.verify_pane.return_value = False  # dead pane

    execute_recovery(
        db,
        mgr,
        live,
        "",
        recovery_dir=tmp_path,
        session_id="sess",
    )

    # Must NOT spawn a new agent
    mgr.spawn_agent.assert_not_called()
    # Must file a high-priority action item
    db.add_action_item.assert_called()
    call_kwargs = db.add_action_item.call_args[1]
    assert call_kwargs["priority"] == "high"
    # Must log the watchdog event
    db.add_watchdog_event.assert_called()
    event_kwargs = db.add_watchdog_event.call_args[1]
    assert event_kwargs["event_type"] == "retry_blocked"
