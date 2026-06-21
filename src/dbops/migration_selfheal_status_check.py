"""Migration 45 (selfheal-triage-v2 P1, 2026-06-21) — drop error_events.status CHECK.

Extracted to its own module so dbops/migrations_recent.py stays within the
loc_gate budget. Self-contained: rebuilds the table to drop the status CHECK,
reproduces every index, idempotent, and FAIL-LOUD on lock.
"""
from __future__ import annotations

import logging
import sqlite3

from dbops.schema import CREATE_ERROR_EVENTS

_log = logging.getLogger(__name__)


def migrate_45_drop_status_check(conn: sqlite3.Connection) -> None:
    """Drop the status CHECK on error_events so new statuses (non_issue,
    non_issue_proposed) are legal.

    SQLite cannot ALTER a CHECK, so rebuild the table inside one BEGIN IMMEDIATE,
    reproducing every index. Idempotent (no-op once the CHECK is gone) and
    FAIL-LOUD on lock — unlike the exception-capture allowlist, a locked DB here
    must raise, never be swallowed (spec §6).
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='error_events'"
    ).fetchone()
    if row is None:
        return  # fresh DB without the table yet — CREATE path already CHECK-free
    sql = (row[0] or "").replace(" ", "")
    if "CHECK(statusIN" not in sql:
        return  # already migrated — idempotent no-op

    # BEGIN IMMEDIATE acquires the write lock up front; on a busy DB this raises
    # sqlite3.OperationalError("database is locked") which we deliberately let
    # propagate (fail loud). conn must not be in autocommit-suppressed state.
    prev_isolation = conn.isolation_level
    conn.isolation_level = None  # explicit transaction control
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE error_events RENAME TO error_events_old")
        conn.execute(CREATE_ERROR_EVENTS)  # CHECK-free DDL from schema.py
        conn.execute(
            "INSERT INTO error_events "
            "(id, signature_hash, error_class, exc_type, traceback, entrypoint, "
            "surface, command_args, juggle_ref, count, first_seen, last_seen, "
            "status, action_item_id) "
            "SELECT id, signature_hash, error_class, exc_type, traceback, entrypoint, "
            "surface, command_args, juggle_ref, count, first_seen, last_seen, "
            "status, action_item_id FROM error_events_old"
        )
        conn.execute("DROP TABLE error_events_old")
        # Reproduce every index (mirror of juggle_db.py).
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_error_events_sig "
            "ON error_events(signature_hash) WHERE status != 'resolved'"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_error_events_status "
            "ON error_events(status)"
        )
        conn.execute("COMMIT")
        _log.info("Migration 45: dropped error_events.status CHECK, indices reproduced")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = prev_isolation
