"""Migration 60 (async-land unlanded-state) — additive nodes.submitted_rev column.

Adds ``nodes.submitted_rev`` (TEXT, opaque — never format-validated, SPEC §7.2)
to already-migrated DBs so an async-land backend can record the ticket/rev a
topic was submitted as while it sits in ``integrated-unlanded``. Fresh DBs get
it from CREATE_NODES.

ADDITIVE only, idempotent, presence-guarded, fail-soft (the additive-migration
convention — see migration_59_node_priority.py). Never rebuilds the table.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_60_submitted_rev(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "nodes" not in tables:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "submitted_rev" in cols:
        return
    try:
        conn.execute("ALTER TABLE nodes ADD COLUMN submitted_rev TEXT")
        conn.commit()
        _log.info("Migration 60: nodes.submitted_rev column ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 60 (submitted_rev) skipped: %s", e)
