"""Regression pin: recover a topic wedged in 'running' with NO live agent.

Incident 2026-07-03 (docs/2026-07-03-incident-dispatch-wedge.md, defect 2): a
topic transitioned to 'running' but its worktree/agent never materialised (a
transient `git worktree add` failure during a concurrent dispatch burst). The
member task stayed 'ready' (claimable), yet the topic sat wedged in 'running'
forever — the task-node orphan reconcile only heals kind='task' nodes and
`graph reconcile` protects 'running', so nothing re-claimed it.

This pins the extended orphan-recovery playbook: a kind='topic' node in 'running'
with NO live (busy) bound agent AND a still-claimable member task is reset to
claimable ('open' → recompute promotes it back to 'ready') so the tick re-claims
it. A topic whose agent is still busy is NEVER reset (G4a).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as tp  # noqa: E402
import juggle_graph_dispatch as gd  # noqa: E402
import juggle_graph_reconcile as gr  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


def _age_topic(db, tid, secs):
    """Backdate the topic's updated_at past the staleness cutoff so it becomes an
    orphan-recovery candidate (mirrors the just-dispatched-window guard)."""
    old = (datetime.now(timezone.utc) - timedelta(seconds=secs)).isoformat()
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET updated_at=? WHERE id=? AND kind='topic'",
                     (old, tid))
        conn.commit()


def _seed_running_topic(db, tid="T1", project="INBOX", stale=True):
    """Topic wedged in 'running' with a single 'ready' (claimable) member task —
    the exact post-dispatch-then-crash orphan (no agent ever attached). When
    ``stale`` it is backdated past the staleness cutoff (a genuine wedge)."""
    tp.create_topic(db, topic_id=tid, project_id=project, title=tid)
    nid = f"{tid}-k0"
    g.create_task(db, task_id=nid, project_id=project, title=nid, prompt="p")
    g.set_task_topic(db, nid, tid)
    g.recompute_ready(db, project)          # member task open → ready
    tp.recompute_topic_ready(db, project)   # topic open → ready
    tp.topic_transition(db, tid, "claim")     # ready → dispatching
    tp.topic_transition(db, tid, "dispatch")  # dispatching → running
    assert tp.get_topic(db, tid)["state"] == "running"
    assert g.get_task(db, nid)["state"] == "ready"
    if stale:
        _age_topic(db, tid, gr.RECONCILE_STALE_SECS + 60)
    return tid, nid


class FakeDispatch:
    def __init__(self):
        self.calls = []

    def __call__(self, db, thread_id, prompt, task):
        self.calls.append((thread_id, prompt, task["id"]))


def test_running_topic_no_agent_reset_to_claimable(db):
    """RED on current code: a 'running' topic with no live agent and a claimable
    member task is reset off the wedge so it is re-eligible for dispatch."""
    tid, _ = _seed_running_topic(db)

    healed = gr.reconcile_orphaned_running_topics(db)

    assert tid in healed
    assert tp.get_topic(db, tid)["state"] == "open"  # claimable again


def test_fresh_running_topic_within_cutoff_not_reset(db):
    """A just-dispatched 'running' topic (recent updated_at, agent not yet
    observable) is NOT reset — the staleness cutoff guards the dispatch window."""
    tid, _ = _seed_running_topic(db, stale=False)  # fresh updated_at

    healed = gr.reconcile_orphaned_running_topics(db)

    assert tid not in healed
    assert tp.get_topic(db, tid)["state"] == "running"


def test_running_topic_with_live_agent_not_reset(db):
    """A 'running' topic whose bound agent is still BUSY is NEVER reset (G4a)."""
    tid, _ = _seed_running_topic(db)
    thread_id = db.create_thread("live", session_id="s")
    tp.set_topic_thread(db, tid, thread_id)
    aid = db.create_agent(role="coder", pane_id="%live")
    db.update_agent(aid, status="busy", assigned_thread=thread_id)

    healed = gr.reconcile_orphaned_running_topics(db)

    assert tid not in healed
    assert tp.get_topic(db, tid)["state"] == "running"


def test_tick_recovers_running_topic_orphan(db):
    """Regression pin (end-to-end): seed a running topic with no agent → after a
    single tick it is re-claimed and re-dispatched (RED on current code)."""
    tid, _ = _seed_running_topic(db)
    fake = FakeDispatch()

    stats = gd.graph_tick(db, dispatch_fn=fake)

    assert stats["dispatched"] == [tid]        # re-claimed and re-dispatched
    assert [c[2] for c in fake.calls] == [tid]  # a dispatch actually fired
    assert tp.get_topic(db, tid)["state"] == "running"
