"""Agent death must reach the graph (DA round-2 MAJOR-1, 2026-06-10).

Incident: cmd_fail_agent (persistent, unrecoverable) and the watchdog
retry_blocked give-up path set thread status but never touched the bound
graph node — it stayed 'running' (a PROTECTED state even reload refuses) and
its dependents stalled silently forever. Both paths must fire exec_fail,
propagate blocked-failed, and raise the HIGH action item.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    import juggle_cli_common as common

    monkeypatch.setattr(common, "get_db", lambda: d)
    return d


def _mk_graph(db):
    """a → b (b depends on a)."""
    g.create_node(db, node_id="a", project_id="INBOX", title="Node A", prompt="do a")
    g.create_node(db, node_id="b", project_id="INBOX", title="Node B", prompt="do b")
    g.replace_edges(db, "b", ["a"])
    g.recompute_ready(db, "INBOX")


def _bind_running_thread(db, node_id, session="sessA"):
    tid = db.create_thread("t", session_id=session)
    db.update_thread(tid, agent_task_id="task-1", status="running")
    db._set_session_key_external("session_id", session)
    g.set_node_thread(db, node_id, tid)
    for ev in ("claim", "dispatch"):
        g.node_transition(db, node_id, ev)
    return tid


def _fail(tid, error="agent crashed hard", **kw):
    from juggle_cmd_agents import cmd_fail_agent

    args = argparse.Namespace(
        thread_id=tid,
        error=error,
        failure_type=kw.get("failure_type", "persistent"),
        recovery_dispatched=kw.get("recovery_dispatched", False),
        max_retries=0,
    )
    cmd_fail_agent(args)


# ── path 1: cmd_fail_agent (persistent, no recovery) ──────────────────────────


def test_fail_agent_unrecoverable_marks_node_failed_exec_and_blocks(db):
    """REGRESSION PIN (DA round-2 MAJOR-1, 2026-06-10): cmd_fail_agent closed
    the thread but left the bound node 'running' — dependents stalled silently.
    Unrecoverable failure must mark the node failed-exec, block dependents,
    and raise the HIGH action item."""
    _mk_graph(db)
    tid = _bind_running_thread(db, "a")
    _fail(tid)

    assert g.get_node(db, "a")["state"] == "failed-exec"
    assert g.get_node(db, "b")["state"] == "blocked-failed"
    items = db.get_open_action_items()
    assert any(
        "failed-exec" in i["message"] and "b" in i["message"] and i["priority"] == "high"
        for i in items
        if "Graph node" in i["message"]
    )


def test_fail_agent_transient_leaves_node_running(db):
    """Transient failures keep the thread (and node) running for retry — the
    graph must NOT be failed."""
    _mk_graph(db)
    tid = _bind_running_thread(db, "a")
    _fail(tid, failure_type="transient")
    assert g.get_node(db, "a")["state"] == "running"
    assert g.get_node(db, "b")["state"] == "pending"


def test_fail_agent_recovery_dispatched_leaves_node_running(db):
    """A recovery dispatch means the node is still being worked — not failed."""
    _mk_graph(db)
    tid = _bind_running_thread(db, "a")
    _fail(tid, recovery_dispatched=True)
    assert g.get_node(db, "a")["state"] == "running"
    assert g.get_node(db, "b")["state"] == "pending"


def test_fail_agent_unbound_thread_untouched(db):
    """A thread with no bound node fails exactly as before."""
    tid = db.create_thread("plain", session_id="sessA")
    db.update_thread(tid, status="running")
    db._set_session_key_external("session_id", "sessA")
    _fail(tid)
    assert db.get_thread(tid)["status"] == "closed"


# ── path 2: watchdog give-up (retry exhausted) ─────────────────────────────────


def test_watchdog_giveup_marks_node_failed_exec_and_blocks(db, tmp_path):
    """REGRESSION PIN (DA round-2 MAJOR-1, 2026-06-10): the watchdog
    retry_blocked give-up path set thread status='failed' but never touched
    the node — it stayed 'running' and dependents stalled silently."""
    from juggle_watchdog import execute_recovery

    _mk_graph(db)
    tid = _bind_running_thread(db, "a")

    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=tid,
        last_task="do work",
        watchdog_retried=1,  # retry already burned → give-up branch
    )
    mgr = MagicMock()
    mgr.verify_pane.return_value = False  # dead pane → recovery path

    execute_recovery(
        db,
        mgr,
        db.get_agent(agent_id),
        "pane content",
        recovery_dir=tmp_path / "recovery",
        session_id="sessA",
    )

    assert g.get_node(db, "a")["state"] == "failed-exec"
    assert g.get_node(db, "b")["state"] == "blocked-failed"
    items = db.get_open_action_items()
    assert any(
        "failed-exec" in i["message"] and i["priority"] == "high"
        for i in items
        if "Graph node" in i["message"]
    )
