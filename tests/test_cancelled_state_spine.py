"""Phase2 cancelled-state correctness spine (2026-07-03, docs §DA-B1/B2/B4).

Pins the `cancelled` terminal state end to end:
  * the node state machine reaches `cancelled` via a `cancel` event and un-cancels
    via `reload` (B4);
  * `cancelled` counts as a COMPLETION terminal at the five rollup sites (B1) —
    reconcile, topic-derive, the complete-agent gate, cockpit dag-is-done, and the
    frontier open filter — so dependents/rollups don't stall;
  * cockpit open-count SQL drops cancelled from "open" and counts it as done (B2);
  * `cancelled` is NEVER added to any blocking/failed set (B1).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dbops.db_node_machine import (  # noqa: E402
    InvalidTransition,
    legal_events,
    node_transition,
)


# ── B4: transition table — cancel event + cancelled reachable + un-cancel ────────

def test_cancel_from_open_reaches_cancelled():
    assert node_transition("open", "cancel", "task") == "cancelled"


def test_cancel_from_failed_verify_reaches_cancelled():
    assert node_transition("failed-verify", "cancel", "task") == "cancelled"


def test_cancel_from_blocked_failed_reaches_cancelled():
    assert node_transition("blocked-failed", "cancel", "task") == "cancelled"


def test_uncancel_via_reload_returns_to_open():
    assert node_transition("cancelled", "reload", "task") == "open"


def test_cancel_is_legal_for_task_kind():
    assert "cancel" in legal_events("task")


def test_cancel_refused_from_inflight_running():
    """In-flight states are never cancellable (no table entry)."""
    with pytest.raises(InvalidTransition):
        node_transition("running", "cancel", "task")


def test_cancel_refused_from_verified():
    with pytest.raises(InvalidTransition):
        node_transition("verified", "cancel", "task")


# ── B1: cancelled is a COMPLETION terminal (not blocking/failed) ─────────────────

def test_topic_derive_cancelled_is_merged_terminal():
    from juggle_topic_derive import derive_topic_state

    assert derive_topic_state(
        ["cancelled"], minutes_since_human_msg=40, close_idle_min=30
    ) == "done"
    assert derive_topic_state(
        ["verified", "cancelled"], minutes_since_human_msg=40, close_idle_min=30
    ) == "done"


def test_task_terminal_set_includes_cancelled():
    from juggle_cmd_agents_graph_topics import _TASK_TERMINAL

    assert "cancelled" in _TASK_TERMINAL


def test_dag_is_done_counts_cancelled_as_done():
    from juggle_cockpit_graph_layout import GraphTask, dag_is_done

    tasks = [
        GraphTask(id="a", title="A", state="verified"),
        GraphTask(id="b", title="B", state="cancelled"),
    ]
    assert dag_is_done(tasks) is True


def test_frontier_excludes_cancelled_from_open_rows():
    from juggle_cockpit_graph_layout import GraphTask
    from juggle_frontier_layout import build_frontier_layout

    tasks = [
        GraphTask(id="a", title="A", state="verified"),
        GraphTask(id="b", title="B", state="cancelled"),
    ]
    layout = build_frontier_layout(tasks, [], free_slots=2)
    assert layout.rows == ()


# ── B1 negative pins: cancelled must NOT join any blocking/failed set ────────────

def test_cancelled_not_in_blocking_states():
    from dbops.db_graph_marking import _BLOCKING_STATES

    assert "cancelled" not in _BLOCKING_STATES


def test_cancelled_not_in_frontier_failed_states():
    from juggle_frontier_layout import FAILED_STATES

    assert "cancelled" not in FAILED_STATES


def test_cancelled_not_in_reconcile_failed_set():
    from dbops import db_topics  # noqa: F401  (load first: avoids circular import)
    from dbops.db_topics_reconcile import _FAILED_TASK_STATES

    assert "cancelled" not in _FAILED_TASK_STATES


# ── DB-backed pins: reconcile rollup + cockpit open-count SQL ────────────────────

def _make_db(tmp_path):
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


def _seed_topic(db, topic_id, task_states, *, state="integrating"):
    from dbops import db_graph, db_topics

    db_topics.create_topic(db, topic_id=topic_id, project_id="INBOX",
                           title=f"Topic {topic_id}")
    with db._connect() as c:
        c.execute("UPDATE nodes SET state=? WHERE id=? AND kind='topic'",
                  (state, topic_id))
        c.commit()
    for i, st in enumerate(task_states):
        tid = f"{topic_id}-t{i}"
        db_graph.create_task(db, task_id=tid, project_id="INBOX", title=tid, prompt="x")
        db_graph.set_task_topic(db, tid, topic_id)
        with db._connect() as c:
            c.execute("UPDATE nodes SET state=? WHERE id=? AND kind='task'", (st, tid))
            c.commit()


def test_reconcile_verified_plus_cancelled_is_not_open_or_failed(tmp_path):
    """B1: a topic whose tasks are all verified-or-cancelled is a completion
    terminal — reconcile must not demote it to 'open' nor mark 'failed-verify'."""
    from dbops.db_topics_reconcile import reconcile_topic_state

    db = _make_db(tmp_path)
    _seed_topic(db, "T1", ["verified", "cancelled"], state="integrating")
    derived = reconcile_topic_state(db, "T1")
    assert derived not in ("open", "failed-verify", "running")


def _seed_project(db, pid, root_task_states):
    from datetime import datetime, timezone

    from dbops import db_graph

    now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc).isoformat()
    with db._connect() as c:
        c.execute(
            "INSERT INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)", (pid, pid, "active", now, now)
        )
        c.commit()
    for i, st in enumerate(root_task_states):
        tid = f"{pid}-r{i}"
        db_graph.create_task(db, task_id=tid, project_id=pid, title=tid, prompt="x")
        with db._connect() as c:
            c.execute("UPDATE nodes SET state=? WHERE id=? AND kind='task'", (st, tid))
            c.commit()


def test_open_count_drops_cancelled(tmp_path):
    """B2 regression pin: a project whose only non-verified root is cancelled reads
    as done (cancelled excluded from open-count at graph_dag.py:83)."""
    from juggle_cockpit_graph_dag import gather_project_activity

    db = _make_db(tmp_path)
    _seed_project(db, "P1", ["verified", "cancelled"])
    with db._connect() as conn:
        rows = {r.id: r for r in gather_project_activity(conn)}
    assert rows["P1"].is_done is True


def test_child_done_count_includes_cancelled(tmp_path):
    """B2: per-topic done-count counts cancelled children as done (graph_dag.py:185)."""
    from juggle_cockpit_graph_dag import _load_one

    db = _make_db(tmp_path)
    _seed_topic(db, "T2", ["verified", "cancelled"], state="integrating")
    with db._connect() as conn:
        dag = _load_one(conn, "INBOX")
    topic = next(t for t in dag.tasks if t.id == "T2")
    assert topic.tasks_done == 2
    assert topic.tasks_total == 2
