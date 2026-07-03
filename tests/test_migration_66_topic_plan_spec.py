"""Migration 66 (T-fix-dispatch-plan-spec-provision) — additive
``nodes.plan_path``/``nodes.spec_path`` columns.

GAP (2026-07-03): dispatch prompts carried no reference to the plan/spec file
a topic's tasks implement — coders were dispatched blind. plan_path/spec_path
are first-class topic-node columns so the dispatch-prompt builder can surface
them.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dbops.migration_66_topic_plan_spec import migrate_66_topic_plan_spec  # noqa: E402


def _bare_nodes_db(conn):
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, title TEXT, "
        "state TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute("INSERT INTO nodes(id, kind) VALUES ('t1', 'topic')")
    conn.commit()


def test_migration_adds_plan_and_spec_path_columns():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _bare_nodes_db(conn)

    migrate_66_topic_plan_spec(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    assert "plan_path" in cols
    assert "spec_path" in cols


def test_migration_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _bare_nodes_db(conn)

    migrate_66_topic_plan_spec(conn)
    migrate_66_topic_plan_spec(conn)  # must not raise on re-run

    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    assert "plan_path" in cols and "spec_path" in cols


def test_migration_noop_without_nodes_table():
    conn = sqlite3.connect(":memory:")
    migrate_66_topic_plan_spec(conn)  # must not raise
