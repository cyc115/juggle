"""TDD tests for lazy DAG loading in the cockpit snapshot.

snapshot(db, load_graph_dag=True) loads the armed project's tasks+edges into
CockpitState.graph_dag. When load_graph_dag is False (default) NO task/edge
DAG query runs — zero cost when graph mode is off. No armed project → None.
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
    db.set_setting(ARMED_PROJECT_KEY, "proj1")
    state = snapshot(db)  # load_graph_dag defaults False
    assert getattr(state, "graph_dag", None) is None


def test_dag_loaded_for_armed_project_when_flag_on(db):
    _make_project_graph(db)
    db.set_setting(ARMED_PROJECT_KEY, "proj1")
    state = snapshot(db, load_graph_dag=True)
    dag = state.graph_dag
    assert dag is not None
    assert dag.project_id == "proj1"
    assert {n.id for n in dag.tasks} == {"proj1-a", "proj1-b"}
    assert ("proj1-b", "proj1-a") in dag.edges


def test_dag_none_when_no_armed_project(db):
    _make_project_graph(db)
    # no ARMED_PROJECT_KEY set
    state = snapshot(db, load_graph_dag=True)
    assert state.graph_dag is None


def test_dag_only_armed_project_tasks(db):
    _make_project_graph(db, "proj1")
    _make_project_graph(db, "proj2")
    db.set_setting(ARMED_PROJECT_KEY, "proj2")
    state = snapshot(db, load_graph_dag=True)
    assert state.graph_dag.project_id == "proj2"
    # both projects use ids a/b — assert it queried proj2 only (2 tasks)
    assert len(state.graph_dag.tasks) == 2


def test_no_extra_query_when_flag_off(db):
    """Spy: the graph_tasks-by-project DAG query must not fire when flag off."""
    _make_project_graph(db)
    db.set_setting(ARMED_PROJECT_KEY, "proj1")

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
       P2: topic C with 1 task. Settings key 'P1,P2'."""
    from dbops import db_graph as g, db_topics as tp
    for pid in ("P1", "P2"):
        with db._connect() as conn:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
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
    with db._connect() as conn:
        conn.execute("UPDATE graph_tasks SET topic_id='A' WHERE id IN ('a1','a2')")
        conn.execute("UPDATE graph_tasks SET topic_id='B' WHERE id='b1'")
        conn.execute("UPDATE graph_tasks SET topic_id='C' WHERE id='c1'")
        conn.execute("UPDATE graph_tasks SET state='verified' WHERE id='a1'")
        # task edge b1 → a1 → crosses B→A boundary
        conn.execute("INSERT INTO graph_edges(task_id,depends_on_id) VALUES('b1','a1')")
        conn.commit()
    db.set_setting(ARMED_PROJECT_KEY, "P1,P2")


def test_load_graph_dags_topics_are_the_dag_tasks(db):
    """REGRESSION PIN (2026-06-11 R5/R9): the loader rendered TASKS as DAG
    tasks and read the armed key as a scalar. Tasks must be TOPICS with task
    progress; edges the DERIVED topic deps; one GraphDag per armed project
    (CSV), arm order."""
    from juggle_cockpit_graph_dag import load_graph_dags

    _seed_two_project_topics(db)
    dags = load_graph_dags(db._connect())
    assert [d.project_id for d in dags] == ["P1", "P2"]
    assert {n.id for n in dags[0].tasks} == {"A", "B"}
    assert dags[0].edges == [("B", "A")]
    a_task = next(n for n in dags[0].tasks if n.id == "A")
    assert a_task.tasks_done == 1 and a_task.tasks_total == 2
    assert "a1" in dags[0].member_tasks["A"] or any(
        t["id"] == "a1" for t in dags[0].member_tasks["A"]
    )


def test_load_graph_dag_shim_returns_first(db):
    from juggle_cockpit_graph_dag import load_graph_dag

    _seed_two_project_topics(db)
    dag = load_graph_dag(db._connect())
    assert dag is not None and dag.project_id == "P1"


def test_load_graph_dags_empty_when_disarmed(db):
    from juggle_cockpit_graph_dag import load_graph_dags

    _seed_two_project_topics(db)
    db.set_setting(ARMED_PROJECT_KEY, None)
    assert load_graph_dags(db._connect()) == []
