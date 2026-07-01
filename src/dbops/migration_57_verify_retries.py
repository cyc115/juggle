"""Migration 57 (verify-fallback self-heal) — additive verify-retry columns.

Adds ``nodes.verify_retries`` (bounded-retry counter) and ``nodes.verify_failure``
(the prior verify_cmd failure output injected into the fresh re-dispatch prompt)
to already-migrated DBs. Fresh DBs get them from CREATE_NODES.

ADDITIVE only, idempotent, presence-guarded, fail-soft (the additive-migration
convention). Never rebuilds the table.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)

_ADDS = [
    ("verify_retries",
     "ALTER TABLE nodes ADD COLUMN verify_retries INTEGER NOT NULL DEFAULT 0"),
    ("verify_failure", "ALTER TABLE nodes ADD COLUMN verify_failure TEXT"),
]


def migrate_57_verify_retries(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "nodes" not in tables:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    try:
        for name, ddl in _ADDS:
            if name not in cols:
                conn.execute(ddl)
        conn.commit()
        _log.info("Migration 57: nodes verify-retry columns ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 57 (verify-retries) skipped: %s", e)
