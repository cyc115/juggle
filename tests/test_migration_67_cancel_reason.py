"""Migration 67 (T-gp-migration, graph-node-primitives Phase 1) — additive
``nodes.cancel_reason`` column.

Backs `graph cancel-node --reason TXT` (DA-B3): the reason string is stored on
the node so the audit row survives a soft-terminal `cancelled` transition.
ADDITIVE only, idempotent, presence-guarded, fail-soft (matches Migration 66).
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dbops.migration_67_cancel_reason import migrate_67_cancel_reason  # noqa: E402


def _bare_nodes_db(conn):
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, title TEXT, "
        "state TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute("INSERT INTO nodes(id, kind) VALUES ('t1', 'topic')")
    conn.commit()


def test_migration_adds_cancel_reason_column():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _bare_nodes_db(conn)

    migrate_67_cancel_reason(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    assert "cancel_reason" in cols


def test_migration_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _bare_nodes_db(conn)

    migrate_67_cancel_reason(conn)
    migrate_67_cancel_reason(conn)  # must not raise on re-run

    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    assert "cancel_reason" in cols


def test_migration_noop_without_nodes_table():
    conn = sqlite3.connect(":memory:")
    migrate_67_cancel_reason(conn)  # must not raise
