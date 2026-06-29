"""Agent death must reach the graph (DA round-2 MAJOR-1, 2026-06-10).

Incident: cmd_fail_agent (persistent, unrecoverable) and the watchdog
retry_blocked give-up path set thread status but never touched the bound
graph task — it stayed 'running' (a PROTECTED state even reload refuses) and
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
    g.create_task(db, task_id="a", project_id="INBOX", title="Task A", prompt="do a")
    g.create_task(db, task_id="b", project_id="INBOX", title="Task B", prompt="do b")
    g.replace_edges(db, "b", ["a"])
    g.recompute_ready(db, "INBOX")


def _bind_running_thread(db, task_id, session="sessA"):
    tid = db.create_thread("t", session_id=session)
    db.update_thread(tid, agent_task_id="task-1", status="running")
    db._set_session_key_external("session_id", session)
    g.set_task_thread(db, task_id, tid)
    for ev in ("claim", "dispatch"):
        g.task_transition(db, task_id, ev)
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


def test_fail_agent_unrecoverable_marks_task_failed_exec_and_blocks(db):
    """REGRESSION PIN (DA round-2 MAJOR-1, 2026-06-10): cmd_fail_agent closed
    the thread but left the bound task 'running' — dependents stalled silently.
    Unrecoverable failure must mark the task failed-exec, block dependents,
    and raise the HIGH action item."""
    _mk_graph(db)
    tid = _bind_running_thread(db, "a")
    _fail(tid)

    assert g.get_task(db, "a")["state"] == "failed-exec"
    assert g.get_task(db, "b")["state"] == "blocked-failed"
    items = db.get_open_action_items()
    assert any(
        "failed-exec" in i["message"] and "b" in i["message"] and i["priority"] == "high"
        for i in items
        if "Graph task" in i["message"]
    )


def test_fail_agent_transient_leaves_task_running(db):
    """Transient failures keep the thread (and task) running for retry — the
    graph must NOT be failed."""
    _mk_graph(db)
    tid = _bind_running_thread(db, "a")
    _fail(tid, failure_type="transient")
    assert g.get_task(db, "a")["state"] == "running"
    assert g.get_task(db, "b")["state"] == "open"


def test_fail_agent_recovery_dispatched_leaves_task_running(db):
    """A recovery dispatch means the task is still being worked — not failed."""
    _mk_graph(db)
    tid = _bind_running_thread(db, "a")
    _fail(tid, recovery_dispatched=True)
    assert g.get_task(db, "a")["state"] == "running"
    assert g.get_task(db, "b")["state"] == "open"


def test_fail_agent_unbound_thread_untouched(db):
    """A thread with no bound task fails exactly as before."""
    tid = db.create_thread("plain", session_id="sessA")
    db.update_thread(tid, status="running")
    db._set_session_key_external("session_id", "sessA")
    _fail(tid)
    assert db.get_thread(tid)["status"] == "closed"


# ── path 2: watchdog give-up (retry exhausted) ─────────────────────────────────


def test_watchdog_giveup_marks_task_failed_exec_and_blocks(db, tmp_path):
    """REGRESSION PIN (DA round-2 MAJOR-1, 2026-06-10): the watchdog
    retry_blocked give-up path set thread status='failed' but never touched
    the task — it stayed 'running' and dependents stalled silently."""
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

    assert g.get_task(db, "a")["state"] == "failed-exec"
    assert g.get_task(db, "b")["state"] == "blocked-failed"
    items = db.get_open_action_items()
    assert any(
        "failed-exec" in i["message"] and i["priority"] == "high"
        for i in items
        if "Graph task" in i["message"]
    )


# ── topic-bound agent death (R9, 2026-06-11) ──────────────────────────────────

from dbops import db_topics as tp  # noqa: E402


def _bind_running_topic(db, topic_id, session="sessA"):
    tid = db.create_thread("t", session_id=session)
    db.update_thread(tid, agent_task_id="task-1", status="running")
    db._set_session_key_external("session_id", session)
    tp.set_topic_thread(db, topic_id, tid)
    for ev in ("deps_ready", "claim", "dispatch"):
        tp.topic_transition(db, topic_id, ev)
    return tid


def test_fail_agent_topic_thread_fails_topic_and_preserves_task_states(db):
    """(adapted to topics, R9 2026-06-11) Agent death on a TOPIC thread fails
    the TOPIC (failed-exec) + blocks derived dependents, but per-task states are
    PRESERVED (the resume story, spec DA A9)."""
    tp.create_topic(db, topic_id="A", project_id="INBOX", title="A")
    tp.create_topic(db, topic_id="B", project_id="INBOX", title="B")
    for n, topic in (("a1", "A"), ("a2", "A"), ("b1", "B")):
        g.create_task(db, task_id=n, project_id="INBOX", title=n, prompt="p")
        g.set_task_topic(db, n, topic)  # dual-writes nodes.parent_id (P8 Task 4.2)
    with db._connect() as conn:  # B derives on A (b1 → a1)
        conn.execute("INSERT INTO graph_edges (task_id, depends_on_id) "
                     "VALUES ('b1','a1')")
        conn.execute("INSERT OR IGNORE INTO node_edges (node_id, depends_on_id) "
                     "VALUES ('b1','a1')")
        conn.commit()
    g.mark_completion(db, "a1", integrate_ok=True, verify_ok=True, handoff="h")
    tid = _bind_running_topic(db, "A")  # pending → deps_ready → … → running

    _fail(tid)

    assert tp.get_topic(db, "A")["state"] == "failed-exec"
    assert tp.get_topic(db, "B")["state"] == "blocked-failed"
    # per-task states untouched — resume story (DA A9)
    assert g.get_task(db, "a1")["state"] == "verified"
    assert g.get_task(db, "a2")["state"] == "open"
    items = db.get_open_action_items()
    assert any("failed-exec" in i["message"] and i["priority"] == "high"
               for i in items if "Topic" in i["message"])
