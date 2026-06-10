"""Tests for juggle_watchdog pure functions."""

import sys
from pathlib import Path
from unittest.mock import MagicMock


sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


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
    # Cold-start floor is max(role_default, _MIN_STALL_THRESHOLD_SECS=600).
    from juggle_watchdog import get_threshold_seconds

    db = MagicMock()
    db.get_median_duration_secs.return_value = None
    agent = {"watchdog_threshold_minutes": None, "role": "coder"}
    assert get_threshold_seconds(db, agent) == 600.0


def test_get_threshold_coldstart_planner():
    # planner floor=300s; cold-start default=180 → max(180, 300)=300.
    from juggle_watchdog import get_threshold_seconds

    db = MagicMock()
    db.get_median_duration_secs.return_value = None
    agent = {"watchdog_threshold_minutes": None, "role": "planner"}
    assert get_threshold_seconds(db, agent) == 300.0


def test_get_threshold_adaptive():
    # 2*90=180 < coder floor 600 → clamped to 600.
    from juggle_watchdog import get_threshold_seconds

    db = MagicMock()
    db.get_median_duration_secs.return_value = 90.0
    agent = {"watchdog_threshold_minutes": None, "role": "coder"}
    assert get_threshold_seconds(db, agent) == 600.0


def test_get_threshold_adaptive_floor_small_median():
    # With a small median (fast fleet), coder threshold must not collapse below 600s.
    from juggle_watchdog import get_threshold_seconds

    db = MagicMock()
    db.get_median_duration_secs.return_value = 30.0
    agent = {"watchdog_threshold_minutes": None, "role": "coder"}
    result = get_threshold_seconds(db, agent)
    assert result == 600.0, f"expected 600.0 coder floor, got {result}"


def test_get_threshold_adaptive_large_median_not_capped():
    # Floor only prevents collapse; a large median must still yield 2*median.
    from juggle_watchdog import get_threshold_seconds

    db = MagicMock()
    db.get_median_duration_secs.return_value = 400.0
    agent = {"watchdog_threshold_minutes": None, "role": "coder"}
    assert get_threshold_seconds(db, agent) == 800.0


def test_get_threshold_adaptive_floor_researcher():
    # researcher floor=180s; small median (10.0) → max(20, 180)=180.
    from juggle_watchdog import get_threshold_seconds

    db = MagicMock()
    db.get_median_duration_secs.return_value = 10.0
    agent = {"watchdog_threshold_minutes": None, "role": "researcher"}
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
    """Recovery silently decommissions agent when last_task is None.

    Old behaviour: filed a high-priority action item.
    New behaviour: silently decommissions — no action item, no thread=failed.
    """
    from juggle_watchdog import execute_recovery
    from juggle_db import JuggleDB

    from datetime import datetime, timezone, timedelta

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%5")
    _old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task=None,
        watchdog_retried=0,
        created_at=_old,
        last_active=_old,
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

    # No action items (silent decommission)
    items = db.get_open_action_items()
    assert items == []
    # Agent must still be deleted (past grace period)
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
    assert any("[RQ]" in it["message"] for it in items)
    assert any("Decide:" in it["message"] for it in items)
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
    # Successful auto-recovery emits notifications only — no action items
    items = db.get_open_action_items()
    assert items == [], f"Expected no action items for auto-recovery, got: {[it['message'] for it in items]}"


# ── alive_slow + closed-thread guard ─────────────────────────────────────────


def test_execute_recovery_alive_slow_closed_thread_idles_agent(tmp_path):
    """alive_slow agent on a CLOSED thread must be idled, not nudged.

    Repro: complete-agent closes the thread but the agent row stays busy.
    Watchdog classifies the pane as alive_slow and previously spammed
    nudge_and_notify every cycle forever.
    Fix: skip nudge, set agent idle/unassigned when assigned thread is closed.
    """
    from juggle_watchdog import execute_recovery
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("test", session_id="")
    db.set_thread_status(thread_id, "closed")

    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="do work",
        watchdog_retried=0,
    )

    mgr = MagicMock()
    mgr.verify_pane.return_value = True  # pane exists → alive_slow
    recovery_dir = tmp_path / "recovery"

    execute_recovery(
        db,
        mgr,
        db.get_agent(agent_id),
        "Working...",  # "Working" is a _CLAUDE_UI_MARKERS entry → alive_slow
        recovery_dir=recovery_dir,
        session_id="",
    )

    # Must NOT nudge — the thread is closed
    mgr._run_tmux.assert_not_called()

    # Agent must be idled and unassigned
    agent = db.get_agent(agent_id)
    assert agent is not None
    assert agent["status"] == "idle"
    assert agent["assigned_thread"] is None


def test_execute_recovery_alive_slow_active_thread_still_nudges(tmp_path):
    """alive_slow agent on an ACTIVE thread still calls nudge (no regression)."""
    from juggle_watchdog import execute_recovery
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("test", session_id="")
    # thread stays "active" (default)

    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="do work",
        watchdog_retried=0,
    )

    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    recovery_dir = tmp_path / "recovery"

    execute_recovery(
        db,
        mgr,
        db.get_agent(agent_id),
        "Working...",
        recovery_dir=recovery_dir,
        session_id="",
    )

    # Must nudge — thread is active
    mgr._run_tmux.assert_called()

    # Agent stays busy (nudge doesn't idle the agent)
    agent = db.get_agent(agent_id)
    assert agent is not None
    assert agent["status"] == "busy"


def test_execute_recovery_alive_slow_no_assigned_thread_no_crash(tmp_path):
    """alive_slow agent with assigned_thread=None must not crash."""
    from juggle_watchdog import execute_recovery
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()

    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=None,
        last_task="do work",
        watchdog_retried=0,
    )

    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    recovery_dir = tmp_path / "recovery"

    # Should not raise
    execute_recovery(
        db,
        mgr,
        db.get_agent(agent_id),
        "Working...",
        recovery_dir=recovery_dir,
        session_id="",
    )

    # No thread → treat as active → nudge called
    mgr._run_tmux.assert_called()


# ── hot-restart: should_hot_restart pure-function tests ──────────────────────


def test_hot_restart_no_change_returns_false():
    from juggle_watchdog import should_hot_restart

    baseline = {"a.py": 1000.0, "b.py": 2000.0}
    current  = {"a.py": 1000.0, "b.py": 2000.0}
    ready, new_lca = should_hot_restart(baseline, current, last_change_at=None, now=5000.0)
    assert ready is False
    assert new_lca is None


def test_hot_restart_change_just_detected_not_ready():
    from juggle_watchdog import should_hot_restart

    baseline = {"a.py": 1000.0}
    current  = {"a.py": 1001.0}
    now = 5000.0
    ready, new_lca = should_hot_restart(baseline, current, last_change_at=None, now=now)
    assert ready is False
    assert new_lca == now  # timestamp recorded


def test_hot_restart_change_within_grace_not_ready():
    from juggle_watchdog import should_hot_restart

    baseline = {"a.py": 1000.0}
    current  = {"a.py": 1001.0}
    last_change_at = 5000.0
    now = 5000.0 + 299.0  # just under 300s
    ready, new_lca = should_hot_restart(baseline, current, last_change_at=last_change_at, now=now)
    assert ready is False
    assert new_lca == last_change_at  # unchanged — no new edit


def test_hot_restart_stable_past_grace_ready():
    from juggle_watchdog import should_hot_restart

    baseline = {"a.py": 1000.0}
    current  = {"a.py": 1001.0}
    last_change_at = 5000.0
    now = 5000.0 + 300.0  # exactly at grace boundary
    ready, new_lca = should_hot_restart(baseline, current, last_change_at=last_change_at, now=now)
    assert ready is True


def test_hot_restart_reverted_to_baseline_cancels():
    from juggle_watchdog import should_hot_restart

    baseline = {"a.py": 1000.0}
    current  = {"a.py": 1000.0}  # reverted
    last_change_at = 5000.0
    now = 6000.0
    ready, new_lca = should_hot_restart(baseline, current, last_change_at=last_change_at, now=now)
    assert ready is False
    assert new_lca is None  # cancelled
