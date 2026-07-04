"""Migration 70 (T-gl-migration, graph-node learnings primitive) — additive
``nodes.learnings`` column.

Backs `graph learn <node-id> "<text>"` (spec 2026-07-03-graph-node-learnings):
a token-light, per-node place for agents to persist decisions/gotchas/constraints
across sessions. ADDITIVE only, idempotent, presence-guarded, fail-soft (matches
Migration 66/67).
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dbops.migration_70_node_learnings import migrate_70_node_learnings  # noqa: E402


def _bare_nodes_db(conn):
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, title TEXT, "
        "state TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute("INSERT INTO nodes(id, kind) VALUES ('t1', 'topic')")
    conn.commit()


def test_migration_adds_learnings_column():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _bare_nodes_db(conn)

    migrate_70_node_learnings(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    assert "learnings" in cols


def test_migration_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _bare_nodes_db(conn)

    migrate_70_node_learnings(conn)
    migrate_70_node_learnings(conn)  # must not raise on re-run

    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    assert "learnings" in cols


def test_migration_noop_without_nodes_table():
    conn = sqlite3.connect(":memory:")
    migrate_70_node_learnings(conn)  # must not raise
