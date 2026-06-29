"""TDD tests for lazy DAG loading in the cockpit snapshot.

snapshot(db, load_graph_dag=True) loads ALL projects' tasks+edges into
CockpitState.graph_dag. When load_graph_dag is False (default) NO task/edge
DAG query runs — zero cost when graph mode is off.

P7: arming concept removed — load_graph_dags returns DAGs for ALL active
projects with tasks, regardless of the autopilot_armed_project settings key.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from juggle_cockpit_model import snapshot  # noqa: E402
from juggle_graph_dispatch import ARMED_PROJECT_KEY  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    d._set_session_key_external("session_id", "sessA")
    return d


def _make_project_graph(db, pid="proj1"):
    with db._connect() as conn:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)",
            (pid, "Proj One", "active", now, now),
        )
        conn.commit()
    a, b = f"{pid}-a", f"{pid}-b"
    g.create_task(db, task_id=a, project_id=pid, title="Setup", prompt="do a")
    g.create_task(db, task_id=b, project_id=pid, title="Build", prompt="do b")
    g.replace_edges(db, b, [a])


def test_dag_not_loaded_when_flag_off(db):
    """Default: graph_dag is None and no DAG query runs."""
    _make_project_graph(db)
    state = snapshot(db)  # load_graph_dag defaults False
    assert getattr(state, "graph_dag", None) is None


def test_dag_loaded_for_project_when_flag_on(db):
    """P7: load_graph_dag=True loads DAGs for all projects with tasks."""
    _make_project_graph(db)
    state = snapshot(db, load_graph_dag=True)
    dag = state.graph_dag
    assert dag is not None
    assert dag.project_id == "proj1"
    assert {n.id for n in dag.tasks} == {"proj1-a", "proj1-b"}
    assert ("proj1-b", "proj1-a") in dag.edges


def test_dag_loaded_without_armed_project_key(db):
    """REGRESSION PIN (P7): DAG is loaded even when autopilot_armed_project
    settings key is absent — arming no longer gates DAG loading."""
    _make_project_graph(db)
    db.set_setting(ARMED_PROJECT_KEY, None)  # explicitly absent
    state = snapshot(db, load_graph_dag=True)
    assert state.graph_dag is not None, (
        "DAG must load without an armed key (P7 — arming removed)"
    )


def test_dag_loads_all_projects(db):
    """P7: load_graph_dags returns DAGs for ALL projects with tasks."""
    _make_project_graph(db, "proj1")
    _make_project_graph(db, "proj2")
    state = snapshot(db, load_graph_dag=True)
    # graph_dag is the first project's DAG (compat shim)
    # The full list is via load_graph_dags
    from juggle_cockpit_graph_dag import load_graph_dags
    with db._connect() as conn:
        dags = load_graph_dags(conn)
    dag_pids = {d.project_id for d in dags}
    assert "proj1" in dag_pids
    assert "proj2" in dag_pids


def test_no_extra_query_when_flag_off(db):
    """Spy: the graph_tasks-by-project DAG query must not fire when flag off."""
    _make_project_graph(db)

    import sqlite3
    real_connect = sqlite3.connect
    seen: list[str] = []

    class _SpyConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            seen.append(sql)
            return self._inner.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def _spy_connect(*a, **k):
        return _SpyConn(real_connect(*a, **k))

    sqlite3.connect = _spy_connect
    try:
        snapshot(db, load_graph_dag=False)
    finally:
        sqlite3.connect = real_connect

    # The DAG-specific edge query references graph_edges; must not appear.
    assert not any("graph_edges" in s for s in seen)


# ── Topic DAG loader (R5/R9, 2026-06-11) ─────────────────────────────────────

def _seed_two_project_topics(db):
    """P1: topics A (2 tasks, 1 verified) and B with a task edge B→A.
       P2: topic C with 1 task.

    P8: also writes nodes (parent nodes for topics, child nodes for tasks)
    and node_edges so _load_one reads from the unified nodes table.
    """
    from dbops import db_graph as g, db_topics as tp
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    for pid in ("P1", "P2"):
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO projects(id,name,status,created_at,last_active) "
                "VALUES(?,?,?,?,?)",
                (pid, pid, "active", now, now),
            )
            conn.commit()
    tp.create_topic(db, topic_id="A", project_id="P1", title="auth")
    tp.create_topic(db, topic_id="B", project_id="P1", title="build")
    tp.create_topic(db, topic_id="C", project_id="P2", title="ci")
    g.create_task(db, task_id="a1", project_id="P1", title="a1", prompt="p")
    g.create_task(db, task_id="a2", project_id="P1", title="a2", prompt="p")
    g.create_task(db, task_id="b1", project_id="P1", title="b1", prompt="p")
    g.create_task(db, task_id="c1", project_id="P2", title="c1", prompt="p")
    # Bind tasks to topics — dual-writes graph_tasks.topic_id + nodes.parent_id
    # (create_task already dual-wrote the child nodes rows; P8 Task 4.1).
    g.set_task_topic(db, "a1", "A")
    g.set_task_topic(db, "a2", "A")
    g.set_task_topic(db, "b1", "B")
    g.set_task_topic(db, "c1", "C")
    with db._connect() as conn:
        conn.execute("UPDATE graph_tasks SET state='verified' WHERE id='a1'")
        conn.execute("UPDATE nodes SET state='verified' WHERE id='a1'")
        # task edge b1 → a1 → crosses B→A boundary (both stores)
        conn.execute("INSERT INTO graph_edges(task_id,depends_on_id) VALUES('b1','a1')")
        conn.execute(
            "INSERT OR IGNORE INTO node_edges(node_id,depends_on_id) VALUES('b1','a1')"
        )
        # Topic parent nodes have no nodes row yet (created via graph_topics only).
        for (nid, pid, title) in [("A","P1","auth"), ("B","P1","build"), ("C","P2","ci")]:
            conn.execute(
                "INSERT OR IGNORE INTO nodes (id,kind,title,objective,state,project_id,"
                "parent_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (nid, "task", title, "", "running", pid, None, now, now),
            )
        conn.commit()


def test_load_graph_dags_topics_are_the_dag_tasks(db):
    """REGRESSION PIN (2026-06-11 R5/R9): the loader rendered TASKS as DAG
    tasks and read the armed key as a scalar. Tasks must be TOPICS with task
    progress; edges the DERIVED topic deps; one GraphDag per project (P7: all
    projects, not just armed ones)."""
    from juggle_cockpit_graph_dag import load_graph_dags

    _seed_two_project_topics(db)
    dags = load_graph_dags(db._connect())
    dag_pids = {d.project_id for d in dags}
    assert "P1" in dag_pids and "P2" in dag_pids

    p1_dag = next(d for d in dags if d.project_id == "P1")
    assert {n.id for n in p1_dag.tasks} == {"A", "B"}
    assert p1_dag.edges == [("B", "A")]
    a_task = next(n for n in p1_dag.tasks if n.id == "A")
    assert a_task.tasks_done == 1 and a_task.tasks_total == 2
    assert "a1" in p1_dag.member_tasks["A"] or any(
        t["id"] == "a1" for t in p1_dag.member_tasks["A"]
    )


def test_load_graph_dag_shim_returns_first(db):
    """load_graph_dag (compat shim) returns first project's DAG."""
    from juggle_cockpit_graph_dag import load_graph_dag

    _seed_two_project_topics(db)
    dag = load_graph_dag(db._connect())
    assert dag is not None
    assert dag.project_id in ("P1", "P2")


def test_load_graph_dags_empty_when_no_projects(db):
    """P7 replacement for old 'disarmed' test: no projects → empty DAG list."""
    from juggle_cockpit_graph_dag import load_graph_dags

    # Fresh DB with no projects → empty list
    dags = load_graph_dags(db._connect())
    assert dags == []


def test_load_graph_dags_returns_all_despite_armed_key_absent(db):
    """REGRESSION PIN (P7): load_graph_dags must return DAGs for all projects
    even when autopilot_armed_project key is NULL — arming is gone."""
    from juggle_cockpit_graph_dag import load_graph_dags

    _seed_two_project_topics(db)
    db.set_setting(ARMED_PROJECT_KEY, None)  # explicitly clear any legacy key
    dags = load_graph_dags(db._connect())
    dag_pids = {d.project_id for d in dags}
    assert "P1" in dag_pids
    assert "P2" in dag_pids
