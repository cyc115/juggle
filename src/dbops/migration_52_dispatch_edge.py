"""Migration 52 (P8 M1/Q2): model the task→dispatch-thread relation as a typed node_edge.

Adds ``node_edges.kind`` (``'dep'`` by default; ``'dispatch'`` for the agent-thread
binding) and backfills the existing ``nodes.dispatch_thread_id`` values into
``kind='dispatch'`` edges ``(node_id=<task node>, depends_on_id=<conversation node>)``
— the value Migration 50 populated from the legacy ``graph_*.thread_id``. After this
migration the live code reads/writes the typed edge exclusively; the raw
``nodes.dispatch_thread_id`` column is retired in the terminal rebuild (Migration 53).

Additive + value-only → fail-SOFT (additive convention, matching Migration 50): the
ALTER is presence-guarded and the dispatch backfill is ``INSERT OR IGNORE``, so a
re-run is a no-op and a benign ``OperationalError`` is logged, never propagated.
Apply via ``juggle doctor`` (behind ``assert_migration_allowed``); never run directly
against the shared prod DB.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_52_dispatch_edge(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "node_edges" not in tables:
        return  # pre-Migration-44 DB — nothing to type yet
    try:
        edge_cols = {r[1] for r in conn.execute("PRAGMA table_info(node_edges)")}
        if "kind" not in edge_cols:
            conn.execute(
                "ALTER TABLE node_edges ADD COLUMN kind TEXT NOT NULL DEFAULT 'dep'")
        # Backfill the typed dispatch edge from the legacy column. Only where the
        # bound conversation node EXISTS, so node_edges stays referentially sane
        # (the FK target must be a real node). INSERT OR IGNORE keeps it idempotent
        # and never clobbers a live binding's edge.
        node_cols = (
            {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
            if "nodes" in tables else set()
        )
        if "dispatch_thread_id" in node_cols:
            conn.execute(
                "INSERT OR IGNORE INTO node_edges (node_id, depends_on_id, kind) "
                "SELECT n.id, n.dispatch_thread_id, 'dispatch' FROM nodes n "
                "WHERE n.kind='task' AND n.dispatch_thread_id IS NOT NULL "
                "  AND n.dispatch_thread_id IN (SELECT id FROM nodes)"
            )
        conn.commit()
        _log.info("Migration 52: node_edges.kind added; dispatch edges backfilled")
    except sqlite3.OperationalError as e:   # fail-soft (additive convention)
        _log.warning("Migration 52 (dispatch edge) skipped: %s", e)
