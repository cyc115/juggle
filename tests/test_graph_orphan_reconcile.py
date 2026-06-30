"""Regression pin: auto-heal of orphaned in-flight graph task nodes.

2026-06-30: task node stuck in 'dispatching' with NULL/dead thread was never
auto-reset (R1 wedge) — required manual stale_reset. The watchdog SKIPS
tick-owned states and the existing orphan recovery (juggle_watchdog.py) operates
on THREADS, not on a task NODE wedged in 'dispatching', so it never self-healed.

This pins the reconcile pass: a kind='task' node wedged in an in-flight state
({dispatching, running, integrating}) with a NULL or DEAD dispatch thread is
auto-healed off the wedge, while a node bound to a LIVE (busy) agent is NEVER
reset (a slow-but-alive agent must survive).
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
import juggle_graph_dispatch as gd  # noqa: E402
import juggle_graph_reconcile as gr  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


def _mk_task(db, task_id, project="INBOX"):
    g.create_task(db, task_id=task_id, project_id=project,
                  title=f"Task {task_id}", prompt=f"do {task_id}")


def _age(db, task_id, secs):
    """Backdate updated_at so the staleness cutoff does not mask the node."""
    old = (datetime.now(timezone.utc) - timedelta(seconds=secs)).isoformat()
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET updated_at=? WHERE id=? AND kind='task'",
                     (old, task_id))
        conn.commit()


def _wedge_dispatching(db, task_id):
    g.recompute_ready(db, "INBOX")          # open → ready
    assert gd.claim_task(db, task_id)        # ready → dispatching, NULL thread
    t = g.get_task(db, task_id)
    assert t["state"] == "dispatching" and t["thread_id"] is None


def test_dispatching_null_thread_is_auto_reset(db):
    """R1 wedge: 'dispatching' + NULL dispatch_thread_id → auto-reset to 'ready'."""
    _mk_task(db, "R1")
    _wedge_dispatching(db, "R1")
    _age(db, "R1", gr.RECONCILE_STALE_SECS + 60)

    healed = gr.reconcile_orphaned_inflight(db)

    assert "R1" in healed
    assert g.get_task(db, "R1")["state"] == "ready"


def test_live_agent_node_is_not_reset(db):
    """A node bound to a LIVE (busy) agent is NEVER reset — even when stale."""
    _mk_task(db, "LIVE")
    g.recompute_ready(db, "INBOX")
    assert gd.claim_task(db, "LIVE")
    tid = db.create_thread("live thread", session_id="s")
    g.set_task_thread(db, "LIVE", tid)
    g.task_transition(db, "LIVE", "dispatch")          # dispatching → running
    aid = db.create_agent(role="coder", pane_id="%live")
    db.update_agent(aid, status="busy", assigned_thread=tid)
    _age(db, "LIVE", gr.RECONCILE_STALE_SECS + 60)     # stale, but agent alive

    healed = gr.reconcile_orphaned_inflight(db)

    assert "LIVE" not in healed
    assert g.get_task(db, "LIVE")["state"] == "running"


def test_running_dead_agent_node_is_failed(db):
    """A 'running' node whose bound thread has no live agent (dead) is healed to
    a failure terminal (NOT silently re-dispatched — its work may have merged)."""
    _mk_task(db, "DEAD")
    g.recompute_ready(db, "INBOX")
    assert gd.claim_task(db, "DEAD")
    tid = db.create_thread("dead thread", session_id="s")
    g.set_task_thread(db, "DEAD", tid)
    g.task_transition(db, "DEAD", "dispatch")          # → running, no busy agent
    _age(db, "DEAD", gr.RECONCILE_STALE_SECS + 60)

    healed = gr.reconcile_orphaned_inflight(db)

    assert "DEAD" in healed
    assert g.get_task(db, "DEAD")["state"] == "failed-exec"


def test_fresh_node_within_cutoff_is_untouched(db):
    """A just-claimed 'dispatching' node (mid-dispatch window) is NOT reset —
    the staleness cutoff guards the bind window."""
    _mk_task(db, "FRESH")
    _wedge_dispatching(db, "FRESH")        # fresh updated_at, NULL thread

    healed = gr.reconcile_orphaned_inflight(db)

    assert "FRESH" not in healed
    assert g.get_task(db, "FRESH")["state"] == "dispatching"
