"""Re-dispatch guard: the stalled/crashed recovery path must NEVER re-dispatch a
topic whose work already LANDED (merged / verified).

2026-07-07 completed agents leak / watchdog re-dispatches merged topic.
Symptom: after a topic's work was INTEGRATED to main, the watchdog re-dispatched
that topic ("coder stalled/crashed — auto-retrying"), spawning a fresh agent for
already-merged work. It wasted a pool slot and a dispatch cycle.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

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
    mgr.spawn_agent = MagicMock(
        return_value={"id": "new-agent-id", "pane_id": "%99", "status": "busy"}
    )
    # Real send_task returns a pane-hash string (2026-07-13 dispatch-stamp fix
    # binds it into the DB) — a bare MagicMock isn't a valid sqlite param.
    mgr.send_task.return_value = "hash-stub"
    return mgr


def _bind_topic(db, tid, topic_id, state, merged_sha):
    from dbops import db_topics as tp
    from dbops.state_write import write_state
    from dbops.schema import _now

    tp.create_topic(db, topic_id=topic_id, project_id="INBOX", title="T")
    tp.set_topic_thread(db, topic_id, tid)
    if merged_sha:
        tp.set_topic_merged_sha(db, topic_id, merged_sha)
    with db._connect() as conn:
        write_state(conn, topic_id, state, now=_now(), verified=(state == "verified"))
        conn.commit()


def test_stalled_agent_on_landed_topic_is_not_redispatched(db, mock_mgr, tmp_path):
    """RED on current code: the stalled path spawns a replacement even though the
    topic already merged. After the fix it releases (no spawn), marks the thread
    done, and files no failure item."""
    tid = db.create_thread("landed", session_id="s")
    db.update_thread(tid, status="background")
    _bind_topic(db, tid, "T-landed", state="verified", merged_sha="deadbeef")

    agent_id = db.create_agent("coder", "%10")
    db.update_agent(
        agent_id, status="busy", assigned_thread=tid,
        last_task="implement feature", watchdog_retried=0,
    )
    agent = db.get_agent(agent_id)

    execute_recovery(
        db, mock_mgr, agent, "Stalled at prompt",
        recovery_dir=tmp_path / "recovery", session_id="s",
    )

    mock_mgr.spawn_agent.assert_not_called()
    assert db.get_agent(agent_id) is None  # stale agent decommissioned
    assert db.get_thread(tid)["state"] == "done"
    items = db.get_open_action_items()
    assert not any(i["type"] == "failure" for i in items), (
        f"landed topic must not file a failure item, got: {items}"
    )


def test_stalled_agent_on_active_topic_still_redispatches(db, mock_mgr, tmp_path):
    """Guard the guard: a genuinely-stalled agent on an ACTIVE (not landed) topic
    must still be re-dispatched — the existing recovery behavior is unchanged."""
    tid = db.create_thread("active", session_id="s")
    db.update_thread(tid, status="background")
    _bind_topic(db, tid, "T-active", state="running", merged_sha=None)

    agent_id = db.create_agent("coder", "%11")
    db.update_agent(
        agent_id, status="busy", assigned_thread=tid,
        last_task="implement feature", watchdog_retried=0,
    )
    agent = db.get_agent(agent_id)

    execute_recovery(
        db, mock_mgr, agent, "Stalled at prompt",
        recovery_dir=tmp_path / "recovery", session_id="s",
    )

    mock_mgr.spawn_agent.assert_called_once()
