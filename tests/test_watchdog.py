"""Tests for juggle_watchdog pure functions."""

import sys
from pathlib import Path
from unittest.mock import MagicMock


sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_classify_working():
    from juggle_watchdog import classify_pane_state

    state, key = classify_pane_state(
        content="new output line\nstill running",
        prev_content="old output",
        stalled_for=0.0,
        threshold=60.0,
    )
    assert state == "working"
    assert key is None


def test_classify_crashed_pane_gone():
    from juggle_watchdog import classify_pane_state

    state, key = classify_pane_state(
        content=None,
        prev_content="some previous",
        stalled_for=0.0,
        threshold=60.0,
    )
    assert state == "crashed"


def test_classify_crashed_shell_prompt():
    from juggle_watchdog import classify_pane_state

    state, key = classify_pane_state(
        content="some output\nmikechen@host:~$ ",
        prev_content="some output\nmikechen@host:~$ ",
        stalled_for=200.0,
        threshold=60.0,
    )
    assert state == "crashed"


def test_classify_prompt_permission():
    from juggle_watchdog import classify_pane_state

    content = "Claude wants to run a command\n1. Yes / 2. Yes, allow always / 3. No"
    state, key = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=300.0,
        threshold=60.0,
    )
    assert state == "prompt"
    assert key == "2"


def test_classify_prompt_plan_mode():
    from juggle_watchdog import classify_pane_state

    content = "Review the plan\n1. Yes, auto-accept / 2. Yes, manually approve / 3. No"
    state, key = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=300.0,
        threshold=60.0,
    )
    assert state == "prompt"
    assert key == "2"


def test_classify_prompt_press_enter():
    from juggle_watchdog import classify_pane_state

    content = "long output\nPress Enter to continue"
    state, key = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=300.0,
        threshold=60.0,
    )
    assert state == "prompt"
    assert key == ""


def test_classify_quiet_thinking():
    from juggle_watchdog import classify_pane_state

    content = "doing stuff\nThinking…"
    state, key = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=300.0,
        threshold=60.0,
    )
    assert state == "quiet"


def test_classify_quiet_within_threshold():
    from juggle_watchdog import classify_pane_state

    state, key = classify_pane_state(
        content="unchanged",
        prev_content="unchanged",
        stalled_for=30.0,
        threshold=120.0,
    )
    assert state == "quiet"


def test_classify_stalled():
    from juggle_watchdog import classify_pane_state

    state, key = classify_pane_state(
        content="unchanged",
        prev_content="unchanged",
        stalled_for=400.0,
        threshold=120.0,
    )
    assert state == "stalled"


# --- Stuck-at-prompt classifier ---


def test_classify_stuck_at_prompt():
    from juggle_watchdog import classify_pane_state, _hash_tail

    content = (
        "╭─────────────────────╮\n│ do something useful │\n╰─────────────────────╯"
    )
    pane_hash = _hash_tail(content)
    state, key = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=90.0,
        threshold=300.0,
        last_send_task_pane_hash=pane_hash,
    )
    assert state == "stuck"
    assert key is None


def test_classify_stuck_not_triggered_within_grace():
    from juggle_watchdog import classify_pane_state, _hash_tail

    content = "╭───╮\n│ x │\n╰───╯"
    pane_hash = _hash_tail(content)
    state, _ = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=30.0,
        threshold=300.0,
        last_send_task_pane_hash=pane_hash,
    )
    assert state == "quiet"


def test_classify_stuck_not_triggered_with_execution_markers():
    from juggle_watchdog import classify_pane_state, _hash_tail

    content = "╭───╮\n│ x │\n╰───╯\n✻ Thinking…"
    pane_hash = _hash_tail(content)
    state, _ = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=120.0,
        threshold=300.0,
        last_send_task_pane_hash=pane_hash,
    )
    assert state == "quiet"


def test_classify_stuck_not_triggered_without_hash():
    from juggle_watchdog import classify_pane_state

    state, _ = classify_pane_state(
        content="unchanged",
        prev_content="unchanged",
        stalled_for=120.0,
        threshold=300.0,
        last_send_task_pane_hash=None,
    )
    assert state == "quiet"


# --- Threshold ---


def test_get_threshold_disabled():
    from juggle_watchdog import get_threshold_seconds

    db = MagicMock()
    agent = {"watchdog_threshold_minutes": -1, "role": "coder"}
    assert get_threshold_seconds(db, agent) == float("inf")


def test_get_threshold_override():
    from juggle_watchdog import get_threshold_seconds

    db = MagicMock()
    agent = {"watchdog_threshold_minutes": 10, "role": "coder"}
    assert get_threshold_seconds(db, agent) == 600.0


def test_get_threshold_coldstart():
    from juggle_watchdog import get_threshold_seconds

    db = MagicMock()
    db.get_median_duration_secs.return_value = None
    agent = {"watchdog_threshold_minutes": None, "role": "coder"}
    assert get_threshold_seconds(db, agent) == 300.0


def test_get_threshold_coldstart_planner():
    from juggle_watchdog import get_threshold_seconds

    db = MagicMock()
    db.get_median_duration_secs.return_value = None
    agent = {"watchdog_threshold_minutes": None, "role": "planner"}
    assert get_threshold_seconds(db, agent) == 180.0


def test_get_threshold_adaptive():
    from juggle_watchdog import get_threshold_seconds

    db = MagicMock()
    db.get_median_duration_secs.return_value = 90.0
    agent = {"watchdog_threshold_minutes": None, "role": "coder"}
    assert get_threshold_seconds(db, agent) == 180.0


# --- Snapshot helpers ---


def test_snapshot_roundtrip(tmp_path):
    from juggle_watchdog import read_snapshot, write_snapshot

    write_snapshot("agent-123", "hello world", snapshot_dir=tmp_path)
    result = read_snapshot("agent-123", snapshot_dir=tmp_path)
    assert result == "hello world"


def test_read_snapshot_missing(tmp_path):
    from juggle_watchdog import read_snapshot

    assert read_snapshot("no-such-agent", snapshot_dir=tmp_path) is None


def test_recovery_snapshot_prune_per_agent(tmp_path):
    """write_recovery_snapshot prunes to 100 files per agent, not globally."""
    from juggle_watchdog import write_recovery_snapshot
    import time

    recovery_dir = tmp_path / "recovery"
    for i in range(105):
        write_recovery_snapshot("agent-A", f"content-{i}", recovery_dir)
        time.sleep(0.001)
    a_files = list(recovery_dir.glob("agent-A-*.txt"))
    assert len(a_files) == 100


# ---------------------------------------------------------------------------
# Recovery tests (Task 4)
# ---------------------------------------------------------------------------


def test_execute_recovery_aborts_if_agent_gone(tmp_path):
    """Recovery aborts if DB recheck shows agent already released (DA-6)."""
    from juggle_watchdog import execute_recovery
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="do work",
        watchdog_retried=0,
    )

    db.update_agent(agent_id, status="idle", assigned_thread=None)

    mgr = MagicMock()
    recovery_dir = tmp_path / "recovery"
    execute_recovery(
        db,
        mgr,
        db.get_agent(agent_id),
        "pane content",
        recovery_dir=recovery_dir,
        session_id="",
    )

    mgr.decommission_agent.assert_not_called()


def test_execute_recovery_no_last_task(tmp_path):
    """Recovery aborts and files action item when last_task is None."""
    from juggle_watchdog import execute_recovery
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task=None,
        watchdog_retried=0,
    )

    mgr = MagicMock()
    recovery_dir = tmp_path / "recovery"
    execute_recovery(
        db,
        mgr,
        db.get_agent(agent_id),
        "pane content",
        recovery_dir=recovery_dir,
        session_id="",
    )

    items = db.get_open_action_items()
    assert any("no task content" in it["message"] for it in items)
    assert db.get_agent(agent_id) is None


def test_execute_recovery_second_stall_blocked(tmp_path):
    """Recovery does not re-dispatch if watchdog_retried == 1."""
    from juggle_watchdog import execute_recovery
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="do work",
        watchdog_retried=1,
    )

    mgr = MagicMock()
    recovery_dir = tmp_path / "recovery"
    execute_recovery(
        db,
        mgr,
        db.get_agent(agent_id),
        "pane content",
        recovery_dir=recovery_dir,
        session_id="",
    )

    items = db.get_open_action_items()
    assert any("stalled AGAIN" in it["message"] for it in items)
    mgr.spawn_agent.assert_not_called()
    # DA-5: thread must be 'failed' even in retry-blocked case
    assert db.get_thread(thread_id)["status"] == "failed"


def test_execute_recovery_full_flow(tmp_path):
    """Successful recovery: decommissions old, spawns new, re-sends task."""
    from juggle_watchdog import execute_recovery
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="do the work",
        watchdog_retried=0,
        model="claude-sonnet-4-6",
    )

    new_agent_id = db.create_agent(role="coder", pane_id="%6")
    new_agent = db.get_agent(new_agent_id)

    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    mgr.spawn_agent.return_value = new_agent
    recovery_dir = tmp_path / "recovery"

    execute_recovery(
        db,
        mgr,
        db.get_agent(agent_id),
        "pane content",
        recovery_dir=recovery_dir,
        session_id="",
    )

    assert db.get_agent(agent_id) is None
    assert db.get_thread(thread_id)["status"] == "background"
    updated_new = db.get_agent(new_agent_id)
    assert updated_new["watchdog_retried"] == 1
    assert updated_new["status"] == "busy"
    mgr.send_task.assert_called_once_with("%6", "do the work")
    items = db.get_open_action_items()
    assert "high" in {it["priority"] for it in items}
    assert any("auto-re-dispatched" in it["message"] for it in items)
