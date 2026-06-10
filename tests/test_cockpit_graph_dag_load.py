"""TDD tests for lazy DAG loading in the cockpit snapshot.

snapshot(db, load_graph_dag=True) loads the armed project's nodes+edges into
CockpitState.graph_dag. When load_graph_dag is False (default) NO node/edge
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
    g.create_node(db, node_id=a, project_id=pid, title="Setup", prompt="do a")
    g.create_node(db, node_id=b, project_id=pid, title="Build", prompt="do b")
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
    assert {n.id for n in dag.nodes} == {"proj1-a", "proj1-b"}
    assert ("proj1-b", "proj1-a") in dag.edges


def test_dag_none_when_no_armed_project(db):
    _make_project_graph(db)
    # no ARMED_PROJECT_KEY set
    state = snapshot(db, load_graph_dag=True)
    assert state.graph_dag is None


def test_dag_only_armed_project_nodes(db):
    _make_project_graph(db, "proj1")
    _make_project_graph(db, "proj2")
    db.set_setting(ARMED_PROJECT_KEY, "proj2")
    state = snapshot(db, load_graph_dag=True)
    assert state.graph_dag.project_id == "proj2"
    # both projects use ids a/b — assert it queried proj2 only (2 nodes)
    assert len(state.graph_dag.nodes) == 2


def test_no_extra_query_when_flag_off(db):
    """Spy: the graph_nodes-by-project DAG query must not fire when flag off."""
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
