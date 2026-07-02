"""Migration 59 (fix-priority dispatch ordering) — additive nodes.priority column.

Adds ``nodes.priority`` (INTEGER NOT NULL DEFAULT 0) to already-migrated DBs so
fix/defect nodes can outrank feature nodes in the ready-dispatch order
(T-fix-priority-dispatch-ordering). Fresh DBs get it from CREATE_NODES.

ADDITIVE only, idempotent, presence-guarded, fail-soft (the additive-migration
convention). Never rebuilds the table.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_59_node_priority(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "nodes" not in tables:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "priority" in cols:
        return
    try:
        conn.execute(
            "ALTER TABLE nodes ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        _log.info("Migration 59: nodes.priority column ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 59 (node priority) skipped: %s", e)
