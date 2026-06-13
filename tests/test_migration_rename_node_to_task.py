"""Migration 39 — rename graph primitive node -> task (T-rename-node-to-task).

REGRESSION PIN (2026-06-13): the project-graph primitive was renamed from
``node`` to ``task``. An EXISTING DB carrying the old schema (``graph_nodes``
table, ``node_id`` columns on ``graph_edges`` and ``agent_runs``) must migrate
in place to ``graph_tasks`` / ``task_id`` with all rows preserved, and the
migration must be idempotent. This file deliberately hand-builds the OLD schema
with literal pre-rename names — do NOT let a rename pass rewrite these literals.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _build_old_schema_db(path: str) -> None:
    """Create a DB with the PRE-rename graph schema and a little data."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE graph_nodes ("
        " id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,"
        " prompt TEXT NOT NULL, verify_cmd TEXT, state TEXT NOT NULL DEFAULT 'pending',"
        " thread_id TEXT, handoff TEXT, diffstat TEXT, verified_at TEXT,"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL, topic_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE graph_edges ("
        " node_id TEXT NOT NULL REFERENCES graph_nodes(id),"
        " depends_on_id TEXT NOT NULL REFERENCES graph_nodes(id),"
        " PRIMARY KEY (node_id, depends_on_id))"
    )
    conn.execute(
        "CREATE TABLE agent_runs ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, project_id TEXT,"
        " topic_id TEXT, node_id TEXT, role TEXT)"
    )
    now = "2026-06-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO graph_nodes (id, project_id, title, prompt, state,"
        " created_at, updated_at, topic_id) VALUES"
        " ('a','INBOX','A','pa','verified',?,?,'T-a'),"
        " ('b','INBOX','B','pb','pending',?,?,'T-b')",
        (now, now, now, now),
    )
    conn.execute(
        "INSERT INTO graph_edges (node_id, depends_on_id) VALUES ('b','a')"
    )
    conn.execute(
        "INSERT INTO agent_runs (thread_id, node_id, role) VALUES ('th-1','a','coder')"
    )
    conn.commit()
    conn.close()


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def test_rename_migration_renames_table_and_columns(tmp_path):
    from dbops.migrations_graph import apply_graph_migrations

    path = str(tmp_path / "old.db")
    _build_old_schema_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    apply_graph_migrations(conn)

    tables = _tables(conn)
    assert "graph_tasks" in tables
    assert "graph_nodes" not in tables
    assert "task_id" in _cols(conn, "graph_edges")
    assert "node_id" not in _cols(conn, "graph_edges")
    assert "task_id" in _cols(conn, "agent_runs")
    assert "node_id" not in _cols(conn, "agent_runs")
    conn.close()


def test_rename_migration_preserves_data(tmp_path):
    from dbops.migrations_graph import apply_graph_migrations

    path = str(tmp_path / "old.db")
    _build_old_schema_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    apply_graph_migrations(conn)

    ids = {r[0] for r in conn.execute("SELECT id FROM graph_tasks").fetchall()}
    assert ids == {"a", "b"}
    edge = conn.execute(
        "SELECT task_id, depends_on_id FROM graph_edges"
    ).fetchone()
    assert (edge[0], edge[1]) == ("b", "a")
    run_task = conn.execute("SELECT task_id FROM agent_runs").fetchone()[0]
    assert run_task == "a"
    conn.close()


def test_rename_migration_idempotent(tmp_path):
    from dbops.migrations_graph import apply_graph_migrations

    path = str(tmp_path / "old.db")
    _build_old_schema_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    apply_graph_migrations(conn)
    apply_graph_migrations(conn)  # re-run must not error or lose data
    ids = {r[0] for r in conn.execute("SELECT id FROM graph_tasks").fetchall()}
    assert ids == {"a", "b"}
    conn.close()


def test_rename_reconciles_when_empty_graph_tasks_already_exists(tmp_path):
    """REGRESSION PIN (2026-06-13): init_db CREATEs an empty graph_tasks BEFORE
    migrations run, so a node-era DB reached migration 39 with BOTH tables and
    the old guard skipped the rename — stranding all rows in graph_nodes. The
    migration must drop the empty shell and rename the populated table."""
    from dbops.migrations_graph import apply_graph_migrations

    path = str(tmp_path / "old.db")
    _build_old_schema_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Simulate init_db pre-creating an EMPTY graph_tasks before migration 39.
    conn.execute(
        "CREATE TABLE graph_tasks ("
        " id TEXT PRIMARY KEY, project_id TEXT, title TEXT, prompt TEXT,"
        " verify_cmd TEXT, state TEXT, thread_id TEXT, handoff TEXT, diffstat TEXT,"
        " verified_at TEXT, created_at TEXT, updated_at TEXT, topic_id TEXT)"
    )
    conn.commit()

    apply_graph_migrations(conn)

    assert "graph_nodes" not in _tables(conn)
    ids = {r[0] for r in conn.execute("SELECT id FROM graph_tasks").fetchall()}
    assert ids == {"a", "b"}, "populated rows must survive, not be stranded"
    conn.close()


def test_fresh_db_has_graph_tasks_not_graph_nodes(tmp_path):
    from juggle_db import JuggleDB

    d = JuggleDB(db_path=str(tmp_path / "fresh.db"))
    d.init_db()
    with d._connect() as conn:
        tables = _tables(conn)
    assert "graph_tasks" in tables
    assert "graph_nodes" not in tables
