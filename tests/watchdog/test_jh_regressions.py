"""Regression tests for JH incident bugs.

Bug 1: Watchdog false-positives undispatched agents as stalled.
Bug 2: execute_recovery crashes on empty last_task or cold-start RuntimeError.
Bug 3: Stale last_task from pool-reused agent.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


# ---------------------------------------------------------------------------
# Bug 1 — classify_pane_state returns awaiting_dispatch for undispatched agents
# ---------------------------------------------------------------------------

def test_classify_pane_state_awaiting_dispatch_when_never_dispatched():
    """Busy agent with last_send_task_at=None must not be classified as stalled."""
    from juggle_watchdog import classify_pane_state

    frozen_content = "Working on it\nstill processing"
    state, key = classify_pane_state(
        content=frozen_content,
        prev_content=frozen_content,  # unchanged → not "working"
        stalled_for=600.0,            # far past any threshold
        threshold=300.0,
        last_send_task_at=None,       # never dispatched
    )
    assert state == "awaiting_dispatch", f"expected awaiting_dispatch, got {state!r}"
    assert key is None


def test_classify_pane_state_stalled_when_dispatched():
    """Dispatched agents with stale content still classify as stalled (no regression)."""
    from juggle_watchdog import classify_pane_state

    frozen_content = "Working on it\nstill processing"
    state, key = classify_pane_state(
        content=frozen_content,
        prev_content=frozen_content,
        stalled_for=600.0,
        threshold=300.0,
        last_send_task_at="2026-05-18T02:00:00+00:00",  # was dispatched
    )
    assert state == "stalled", f"expected stalled, got {state!r}"


# ---------------------------------------------------------------------------
# Bug 2a — execute_recovery with empty last_task does not call send_task
# ---------------------------------------------------------------------------

def test_execute_recovery_empty_last_task_no_send_task(tmp_path):
    """Recovery on agent with last_task=None must not invoke send_task.

    Main behaviour (as of v1.33): silently decommissions if agent age >= boot grace (120s).
    We patch _get_agent_age_secs to return 300s so the decommission path is exercised.
    """
    from unittest.mock import patch
    from juggle_db import JuggleDB
    from juggle_watchdog import execute_recovery
    import juggle_watchdog as _wd

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("test-jh-bug2a", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id,
                    last_task=None, watchdog_retried=0)

    mgr = MagicMock()
    # Bypass the boot-grace check by pretending the agent is 300s old.
    with patch.object(_wd, "_get_agent_age_secs", return_value=300.0):
        execute_recovery(db, mgr, db.get_agent(agent_id), "pane content",
                         recovery_dir=tmp_path / "recovery", session_id="")

    # Core assertion: never-tasked agent must never trigger send_task.
    mgr.send_task.assert_not_called()
    # Agent silently decommissioned (no action item — this is not a user-visible failure).
    assert db.get_agent(agent_id) is None  # agent decommissioned


# ---------------------------------------------------------------------------
# Bug 2b — execute_recovery catches cold-start RuntimeError from send_task
# ---------------------------------------------------------------------------

def test_execute_recovery_cold_start_failure_does_not_raise(tmp_path):
    """RuntimeError from send_task must be caught; poll loop must not crash."""
    from juggle_db import JuggleDB
    from juggle_watchdog import execute_recovery

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("test-jh-bug2b", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id,
                    last_task="do the work", watchdog_retried=0)

    new_agent_id = db.create_agent(role="coder", pane_id="%6")
    new_agent = db.get_agent(new_agent_id)

    mgr = MagicMock()
    mgr.spawn_agent.return_value = new_agent
    mgr.send_task.side_effect = RuntimeError(
        "Claude UI not ready in pane %6 after 30s — aborting send_task"
    )

    # Must not raise — watchdog catches it
    execute_recovery(db, mgr, db.get_agent(agent_id), "pane content",
                     recovery_dir=tmp_path / "recovery", session_id="")

    items = db.get_open_action_items()
    assert any("RECOVERY-COLD-START-FAILED" in it["message"] for it in items)


# ---------------------------------------------------------------------------
# Bug 3 — cmd_release_agent clears last_task after copying to thread
# ---------------------------------------------------------------------------

def test_release_agent_clears_task_state(tmp_path):
    """After release, agent's last_task/last_send_task_at must be NULL; thread gets copy."""
    from juggle_db import JuggleDB
    from juggle_cmd_agents import cmd_release_agent

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    thread_id = db.create_thread("test-jh-bug3", session_id="test-session")
    db.update_thread(thread_id, status="closed")  # allow release without --force

    agent_id = db.create_agent(role="coder", pane_id="%7")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id,
                    last_task="do JG work", last_send_task_at=now,
                    last_send_task_pane_hash="abc123", watchdog_retried=1)

    with db._connect() as conn:
        conn.execute("INSERT OR REPLACE INTO session (key, value) VALUES ('session_id','s1')")
        conn.commit()

    args = MagicMock()
    args.agent_id = agent_id
    args.force = False

    # Patch get_db to return our test db
    import juggle_cmd_agents as _cmd_mod
    original_get_db = _cmd_mod.get_db
    _cmd_mod.get_db = lambda: db
    try:
        cmd_release_agent(args)
    finally:
        _cmd_mod.get_db = original_get_db

    agent = db.get_agent(agent_id)
    assert agent is not None
    assert agent["last_task"] is None, "last_task must be cleared after release"
    assert agent["last_send_task_at"] is None, "last_send_task_at must be cleared"
    assert agent["last_send_task_pane_hash"] is None, "pane_hash must be cleared"
    assert agent["watchdog_retried"] == 0, "watchdog_retried must be reset"

    thread = db.get_thread(thread_id)
    assert thread["last_dispatched_task"] == "do JG work", "thread must receive task copy"
