"""Migration 67 (T-gp-migration, graph-node-primitives Phase 1) — additive
``nodes.cancel_reason`` column.

Backs the forthcoming ``graph cancel-node --reason TXT`` primitive (spec
DA-B3): ``cancel`` is a soft-terminal ``cancelled`` state, never a hard delete,
so the operator's reason string is stored on the node to keep the audit row
intact. ``nodes.state`` has no CHECK constraint, so the new ``cancelled`` state
itself needs no migration — only this column does.

ADDITIVE only, idempotent, presence-guarded, fail-soft (matches Migration 66).
Never rebuilds the table.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_67_cancel_reason(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "nodes" not in tables:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    try:
        if "cancel_reason" not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN cancel_reason TEXT")
        conn.commit()
        _log.info("Migration 67: nodes.cancel_reason column ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 67 (cancel_reason) skipped: %s", e)
