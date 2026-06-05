"""Integration smoke + orphaned detection tests."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


def test_orphaned_thread_files_action_item(db, tmp_path):
    from datetime import datetime, timezone, timedelta
    from juggle_watchdog import check_orphaned_threads

    thread_id = db.create_thread("orphan test", session_id="")
    db.update_thread(thread_id, status="background")
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with db._connect() as conn:
        conn.execute(
            "UPDATE threads SET last_active_at=? WHERE id=?", (past, thread_id)
        )
        conn.commit()

    orphaned = check_orphaned_threads(db, orphan_threshold=300.0)
    assert thread_id in orphaned

    items = db.get_open_action_items()
    assert any("orphaned" in it["message"].lower() for it in items)
    assert "high" in {it["priority"] for it in items}


def test_orphaned_thread_dedup(db, tmp_path):
    from datetime import datetime, timezone, timedelta
    from juggle_watchdog import check_orphaned_threads

    thread_id = db.create_thread("dedup test", session_id="")
    db.update_thread(thread_id, status="background")
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with db._connect() as conn:
        conn.execute(
            "UPDATE threads SET last_active_at=? WHERE id=?", (past, thread_id)
        )
        conn.commit()

    check_orphaned_threads(db, orphan_threshold=300.0)
    check_orphaned_threads(db, orphan_threshold=300.0)

    items = db.get_open_action_items()
    orphan_items = [it for it in items if "orphaned" in it["message"].lower()]
    assert len(orphan_items) == 1


def test_active_thread_not_orphaned(db, tmp_path):
    from datetime import datetime, timezone, timedelta
    from juggle_watchdog import check_orphaned_threads

    thread_id = db.create_thread("active test", session_id="")
    db.update_thread(thread_id, status="background")
    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id)
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with db._connect() as conn:
        conn.execute(
            "UPDATE threads SET last_active_at=? WHERE id=?", (past, thread_id)
        )
        conn.commit()

    orphaned = check_orphaned_threads(db, orphan_threshold=300.0)
    assert thread_id not in orphaned
    assert db.get_open_action_items() == []


def test_full_stall_recovery_cycle(db, tmp_path):
    """Simulate: agent busy → same pane content × threshold → recovery fires."""
    from juggle_watchdog import (
        classify_pane_state,
        execute_recovery,
        get_threshold_seconds,
        write_snapshot,
    )

    thread_id = db.create_thread("integration test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%9")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="do the work",
        watchdog_retried=0,
        watchdog_threshold_minutes=1,
    )

    snapshot_dir = tmp_path / "snapshots"
    recovery_dir = tmp_path / "recovery"
    # Use content with no Claude UI markers and no shell prompt so
    # classify_pane_state returns "stalled" and _classify_agent_state
    # returns "never_fired" — triggering recovery (not nudge).
    pane_content = "processing data\nstep 1 complete\nstep 2 pending"
    write_snapshot(agent_id, pane_content, snapshot_dir)

    agent = db.get_agent(agent_id)
    threshold = get_threshold_seconds(db, agent)
    assert threshold == 60.0

    state, key = classify_pane_state(
        content=pane_content,
        prev_content=pane_content,
        stalled_for=70.0,
        threshold=threshold,
    )
    assert state == "stalled"

    new_agent_id = db.create_agent(role="coder", pane_id="%10")
    new_agent = db.get_agent(new_agent_id)

    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    mgr.spawn_agent.return_value = new_agent

    execute_recovery(
        db, mgr, agent, pane_content, recovery_dir=recovery_dir, session_id=""
    )

    assert db.get_agent(agent_id) is None
    assert db.get_thread(thread_id)["status"] == "background"
    new = db.get_agent(new_agent_id)
    assert new["watchdog_retried"] == 1
    assert new["status"] == "busy"
    snaps = list(recovery_dir.glob(f"{agent_id}-*.txt"))
    assert len(snaps) == 1
    items = db.get_open_action_items()
    assert len(items) == 2
    assert {it["priority"] for it in items} == {"high", "normal"}


def test_orphan_detection_repro(db):
    """Detection repro: background+no-agent+old last_active → flagged; control with busy agent → not flagged."""
    from datetime import datetime, timezone, timedelta
    from juggle_watchdog import check_orphaned_threads

    past = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()

    t1 = db.create_thread("orphan", session_id="")
    db.update_thread(t1, status="background")
    with db._connect() as conn:
        conn.execute("UPDATE threads SET last_active_at=? WHERE id=?", (past, t1))
        conn.commit()

    t2 = db.create_thread("control", session_id="")
    db.update_thread(t2, status="background")
    a2 = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(a2, status="busy", assigned_thread=t2)
    with db._connect() as conn:
        conn.execute("UPDATE threads SET last_active_at=? WHERE id=?", (past, t2))
        conn.commit()

    orphaned = check_orphaned_threads(db, orphan_threshold=300.0)
    assert t1 in orphaned
    assert t2 not in orphaned


def test_orphan_auto_recovery_dispatches(db):
    """Orphan with last_dispatched_task → spawn+send_task called, recovery event + dedup recorded."""
    from datetime import datetime, timezone, timedelta
    from unittest.mock import MagicMock
    from juggle_watchdog import check_orphaned_threads

    past = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    t = db.create_thread("recover me", session_id="")
    db.update_thread(t, status="background")
    with db._connect() as conn:
        conn.execute(
            "UPDATE threads SET last_active_at=?, last_dispatched_task=?, "
            "last_dispatched_role=?, last_dispatched_model=? WHERE id=?",
            (past, "do the work", "coder", "claude-sonnet-4-6", t),
        )
        conn.commit()

    new_agent_id = db.create_agent(role="coder", pane_id="%99")
    new_agent = db.get_agent(new_agent_id)
    mgr = MagicMock()
    mgr.spawn_agent.return_value = new_agent

    orphaned = check_orphaned_threads(db, orphan_threshold=300.0, mgr=mgr)

    assert t in orphaned
    mgr.spawn_agent.assert_called_once()
    mgr.send_task.assert_called_once()

    with db._connect() as conn:
        row = conn.execute(
            "SELECT * FROM watchdog_events WHERE thread_id=? AND event_type='orphan_recovery'",
            (t,),
        ).fetchone()
    assert row is not None

    items = db.get_open_action_items()
    recovery_items = [it for it in items if "re-dispatch" in it["message"].lower() or "recovery" in it["message"].lower()]
    assert recovery_items, f"Expected recovery action item, got: {[it['message'] for it in items]}"


def test_orphan_no_task_falls_back_to_manual(db):
    """No last_dispatched_task → manual action item, spawn NOT called."""
    from datetime import datetime, timezone, timedelta
    from unittest.mock import MagicMock
    from juggle_watchdog import check_orphaned_threads

    past = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    t = db.create_thread("no task", session_id="")
    db.update_thread(t, status="background")
    with db._connect() as conn:
        conn.execute("UPDATE threads SET last_active_at=? WHERE id=?", (past, t))
        conn.commit()

    mgr = MagicMock()
    check_orphaned_threads(db, orphan_threshold=300.0, mgr=mgr)

    mgr.spawn_agent.assert_not_called()
    items = db.get_open_action_items()
    assert any("orphan" in it["message"].lower() for it in items)


def test_orphan_pool_full_falls_back_to_manual(db):
    """Pool at max capacity → spawn NOT called, manual action item filed."""
    from datetime import datetime, timezone, timedelta
    from unittest.mock import MagicMock
    from juggle_watchdog import check_orphaned_threads

    for i in range(20):
        aid = db.create_agent(role="coder", pane_id=f"%{i+10}")
        db.update_agent(aid, status="busy")

    past = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    t = db.create_thread("pool full", session_id="")
    db.update_thread(t, status="background")
    with db._connect() as conn:
        conn.execute(
            "UPDATE threads SET last_active_at=?, last_dispatched_task='do work' WHERE id=?",
            (past, t),
        )
        conn.commit()

    mgr = MagicMock()
    check_orphaned_threads(db, orphan_threshold=300.0, mgr=mgr)
    mgr.spawn_agent.assert_not_called()


def test_orphan_max_attempts_falls_back_to_manual(db):
    """≥2 prior recovery attempts → spawn NOT called, manual action item."""
    from datetime import datetime, timezone, timedelta
    from unittest.mock import MagicMock
    from juggle_watchdog import check_orphaned_threads

    past = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    t = db.create_thread("max attempts", session_id="")
    db.update_thread(t, status="background")
    with db._connect() as conn:
        conn.execute(
            "UPDATE threads SET last_active_at=?, last_dispatched_task='do work' WHERE id=?",
            (past, t),
        )
        conn.commit()

    db.add_watchdog_event(
        agent_id="orphan_detector", thread_id=t,
        event_type="orphan_recovery", snapshot_path=None,
    )
    db.add_watchdog_event(
        agent_id="orphan_detector", thread_id=t,
        event_type="orphan_recovery", snapshot_path=None,
    )

    mgr = MagicMock()
    check_orphaned_threads(db, orphan_threshold=300.0, mgr=mgr)
    mgr.spawn_agent.assert_not_called()
    items = db.get_open_action_items()
    assert any("orphan" in it["message"].lower() or "manual" in it["message"].lower() for it in items)


def test_allowlist_resolution_no_recovery(db, tmp_path):
    """Permission prompt auto-resolved — no recovery, no action item."""
    from juggle_watchdog import classify_pane_state

    thread_id = db.create_thread("permission test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%7")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id)

    content = (
        "Claude wants to run a bash command\n1. Yes / 2. Yes, allow always / 3. No"
    )
    state, key = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=300.0,
        threshold=60.0,
    )
    assert state == "prompt"
    assert key == "2"
    assert db.get_open_action_items() == []
