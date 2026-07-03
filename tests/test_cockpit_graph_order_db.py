"""DB-level tests for cockpit graph ordering: gather_project_activity derives
active/done partition + activity keys from the real nodes tables, and
load_graph_dags renders projects in that order.

Feature spec: 2026-07-03-cockpit-graph-sort. A project with open (non-verified)
root topics is ACTIVE and sorts above a fully-verified DONE project.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g, db_topics as tp  # noqa: E402
from juggle_cockpit_graph_dag import (  # noqa: E402
    gather_project_activity,
    load_graph_dags,
)


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


def _project(db, pid, last_active):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)",
            (pid, pid, "active", last_active, last_active),
        )
        conn.commit()


def _set_topic_state(db, tid, state):
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET state=? WHERE id=? AND kind='topic'", (state, tid)
        )
        conn.commit()


def _seed_active_and_done(db):
    """ACT: topic still running (open work). DONE: topic verified (no open work)."""
    _project(db, "ACT", "2026-07-03 10:00")
    _project(db, "DONE", "2026-07-03 09:00")
    tp.create_topic(db, topic_id="act1", project_id="ACT", title="active topic")
    tp.create_topic(db, topic_id="done1", project_id="DONE", title="done topic")
    g.create_task(db, task_id="at1", project_id="ACT", title="at1", prompt="p")
    g.create_task(db, task_id="dt1", project_id="DONE", title="dt1", prompt="p")
    g.set_task_topic(db, "at1", "act1")
    g.set_task_topic(db, "dt1", "done1")
    _set_topic_state(db, "act1", "running")
    _set_topic_state(db, "done1", "verified")


def test_gather_marks_done_when_all_root_topics_verified(db):
    _seed_active_and_done(db)
    rows = {r.id: r for r in gather_project_activity(db._connect())}
    assert rows["ACT"].is_done is False
    assert rows["DONE"].is_done is True


def test_load_graph_dags_sorts_active_before_done(db):
    """PIN: cockpit-graph-autosort (2026-07-03) — a finished project must sink
    below one with open work in the rendered DAG order."""
    _seed_active_and_done(db)
    dags = load_graph_dags(db._connect())
    pids = [d.project_id for d in dags]
    assert pids.index("ACT") < pids.index("DONE")


def test_load_graph_dags_orders_active_by_recency(db):
    """Two active projects: the more-recently-active one renders first."""
    _project(db, "OLD", "2026-07-01 08:00")
    _project(db, "NEW", "2026-07-03 20:00")
    for pid in ("OLD", "NEW"):
        tp.create_topic(db, topic_id=f"{pid}-t", project_id=pid, title="t")
        g.create_task(db, task_id=f"{pid}-x", project_id=pid, title="x", prompt="p")
        g.set_task_topic(db, f"{pid}-x", f"{pid}-t")
        _set_topic_state(db, f"{pid}-t", "running")
    dags = load_graph_dags(db._connect())
    pids = [d.project_id for d in dags]
    assert pids.index("NEW") < pids.index("OLD")
