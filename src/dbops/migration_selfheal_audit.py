"""Migration 48 (selfheal-triage-v2 P2, 2026-06-21) — selfheal_audit table.

Additive: creates the durable hide-on-arrival audit log. Own module (mirrors mig
45) for the loc_gate budget. Idempotent (CREATE TABLE IF NOT EXISTS).

This table is the HARD precondition for the audited silent auto-hide path
(Task 5): a benign verdict may only be silently hidden once its audit row is
durably recorded.
"""
from __future__ import annotations

import logging
import sqlite3

from dbops.schema import CREATE_SELFHEAL_AUDIT

_log = logging.getLogger(__name__)


def migrate_selfheal_audit(conn: sqlite3.Connection) -> None:
    """Create the selfheal_audit table (+ indexes). Idempotent."""
    try:
        conn.execute(CREATE_SELFHEAL_AUDIT)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_selfheal_audit_action "
            "ON selfheal_audit(action)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_selfheal_audit_sig "
            "ON selfheal_audit(signature_hash)"
        )
        conn.commit()
        _log.info("Migration 48: selfheal_audit table created")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 48 (selfheal_audit) skipped: %s", e)
