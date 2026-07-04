"""Migration 70 (T-gl-migration, graph-node learnings primitive) — additive
``nodes.learnings`` column.

Backs the ``graph learn <node-id> "<text>"`` write primitive (spec
2026-07-03-graph-node-learnings): a free-use, token-light place at the graph-node
level for agents to persist learnings (decisions, gotchas, non-obvious
constraints) across sessions. One overwrite-on-rewrite field per node, ≤300 chars
enforced at the write surface (not here — the column is plain nullable TEXT).

ADDITIVE only, idempotent, presence-guarded, fail-soft (matches Migration 66/67).
Never rebuilds the table.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_70_node_learnings(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "nodes" not in tables:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    try:
        if "learnings" not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN learnings TEXT")
        conn.commit()
        _log.info("Migration 70: nodes.learnings column ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 70 (node_learnings) skipped: %s", e)
