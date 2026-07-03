"""Migration 63 (irl-envelope T2) — additive ``nodes.fail_envelope`` column.

Adds ``nodes.fail_envelope`` (TEXT, nullable) to already-migrated DBs so a
failed `juggle integrate` can persist its classification (class / reason /
attempt / handled_by — see juggle_integrate_envelope.classify) on the bound
graph task. Fresh DBs get it from CREATE_NODES.

ADDITIVE only, idempotent, presence-guarded, fail-soft (matches Migration 59).
Never rebuilds the table.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_63_fail_envelope(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "nodes" not in tables:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "fail_envelope" in cols:
        return
    try:
        conn.execute("ALTER TABLE nodes ADD COLUMN fail_envelope TEXT")
        conn.commit()
        _log.info("Migration 63: nodes.fail_envelope column ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 63 (fail envelope) skipped: %s", e)
