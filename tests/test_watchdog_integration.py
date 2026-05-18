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
        conn.execute("UPDATE threads SET last_active_at=? WHERE id=?", (past, thread_id))
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
        conn.execute("UPDATE threads SET last_active_at=? WHERE id=?", (past, thread_id))
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
        conn.execute("UPDATE threads SET last_active_at=? WHERE id=?", (past, thread_id))
        conn.commit()

    orphaned = check_orphaned_threads(db, orphan_threshold=300.0)
    assert thread_id not in orphaned
    assert db.get_open_action_items() == []


def test_full_stall_recovery_cycle(db, tmp_path):
    """Simulate: agent busy → same pane content × threshold → recovery fires."""
    from juggle_watchdog import (classify_pane_state, execute_recovery,
                                  get_threshold_seconds, write_snapshot)

    thread_id = db.create_thread("integration test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%9")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id,
                    last_task="do the work", watchdog_retried=0,
                    watchdog_threshold_minutes=1)

    snapshot_dir = tmp_path / "snapshots"
    recovery_dir = tmp_path / "recovery"
    pane_content = "Working on stuff\nstill here"
    write_snapshot(agent_id, pane_content, snapshot_dir)

    agent = db.get_agent(agent_id)
    threshold = get_threshold_seconds(db, agent)
    assert threshold == 60.0

    state, key = classify_pane_state(
        content=pane_content, prev_content=pane_content,
        stalled_for=70.0, threshold=threshold,
    )
    assert state == "stalled"

    new_agent_id = db.create_agent(role="coder", pane_id="%10")
    new_agent = db.get_agent(new_agent_id)

    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    mgr.spawn_agent.return_value = new_agent

    execute_recovery(db, mgr, agent, pane_content,
                     recovery_dir=recovery_dir, session_id="")

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


def test_allowlist_resolution_no_recovery(db, tmp_path):
    """Permission prompt auto-resolved — no recovery, no action item."""
    from juggle_watchdog import classify_pane_state

    thread_id = db.create_thread("permission test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%7")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id)

    content = "Claude wants to run a bash command\n1. Yes / 2. Yes, allow always / 3. No"
    state, key = classify_pane_state(
        content=content, prev_content=content,
        stalled_for=300.0, threshold=60.0,
    )
    assert state == "prompt"
    assert key == "2"
    assert db.get_open_action_items() == []
