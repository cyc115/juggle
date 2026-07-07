"""Completed-agent reaper — releases a pool agent left busy while idle at its
prompt on a topic whose work already landed (verified / merged / done).

2026-07-07 completed agents leak / watchdog re-dispatches merged topic.
Symptom: five coder agents whose work had COMPLETED sat status=busy for 15-35h,
idle at their tmux prompt, never released — saturating the pool (5/5) so every
new `agent get` failed "pool full". The stalled/crashed reaper only targets
STALLED/CRASHED agents; there was NO reaper for a COMPLETED-but-unreleased agent
(bound topic terminal, agent idle).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from juggle_db import JuggleDB

# claude-harness markers (SSOT mirror; used as injected fixtures)
READY = ("shift+tab to cycle",)
SUBMIT = ("esc to interrupt", "✻")
IDLE_PANE = "recap: all done.\n\n > \n  shift+tab to cycle"
WORKING_PANE = "Running tests…\n✻ Cooking (26s · esc to interrupt)\n  shift+tab to cycle"


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


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


def _busy_agent_on(db, tid, pane="%1"):
    agent_id = db.create_agent("coder", pane)
    db.update_agent(agent_id, status="busy", assigned_thread=tid, last_task="do it")
    return agent_id


def _reap(db, capture):
    from juggle_watchdog_reap_done import reap_completed_agents

    reap_completed_agents(
        db,
        MagicMock(),
        session_id="s",
        capture=lambda pid: capture,
        markers_for=lambda a: (READY, SUBMIT),
        active_pattern_for=lambda a: "",
    )


def test_completed_topic_idle_agent_is_released(db):
    """Idle-at-prompt agent bound to a landed (verified+merged) topic → reaper
    returns it to the pool (status idle, slot freed)."""
    tid = db.create_thread("done-topic", session_id="s")
    db.update_thread(tid, status="background")
    _bind_topic(db, tid, "T-done", state="verified", merged_sha="deadbeef")
    agent_id = _busy_agent_on(db, tid)

    _reap(db, IDLE_PANE)

    agent = db.get_agent(agent_id)
    assert agent["status"] == "idle"
    assert agent["assigned_thread"] is None


def test_reaper_ignores_agent_mid_turn(db):
    """Guard: an agent actively working (submission marker visible) on a landed
    topic must NOT be reaped even though the topic is terminal."""
    tid = db.create_thread("done-topic", session_id="s")
    db.update_thread(tid, status="background")
    _bind_topic(db, tid, "T-done", state="verified", merged_sha="deadbeef")
    agent_id = _busy_agent_on(db, tid)

    _reap(db, WORKING_PANE)

    assert db.get_agent(agent_id)["status"] == "busy"


def test_reaper_ignores_agent_on_active_topic(db):
    """Guard: an idle agent whose bound topic is still ACTIVE is legitimately
    awaiting its next task — the reaper must leave it alone."""
    tid = db.create_thread("active-topic", session_id="s")
    db.update_thread(tid, status="background")
    _bind_topic(db, tid, "T-active", state="running", merged_sha=None)
    agent_id = _busy_agent_on(db, tid)

    _reap(db, IDLE_PANE)

    assert db.get_agent(agent_id)["status"] == "busy"
