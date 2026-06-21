"""Migration 47 (selfheal-triage-v2 P2, 2026-06-21) — error_events.group_key.

Additive: adds the derived ``group_key`` column + index and backfills it for
existing rows by recomputing ``selfheal_grouping.group_key`` from columns that
are already stored (error_class, exc_type, entrypoint, traceback). Own module so
dbops/migrations_recent.py stays within the loc_gate budget (mirrors mig 45).

Idempotent: the ALTER is PRAGMA-guarded; the index is IF NOT EXISTS; the backfill
only touches rows whose group_key IS NULL.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_group_key(conn: sqlite3.Connection) -> None:
    """Add error_events.group_key (+ index) and backfill NULLs. Idempotent."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='error_events'"
    ).fetchone()
    if row is None:
        return  # fresh DB without the table yet — CREATE path already has the column
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(error_events)").fetchall()}
        if "group_key" not in cols:
            conn.execute("ALTER TABLE error_events ADD COLUMN group_key TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_error_events_group_key "
            "ON error_events(group_key)"
        )
        _backfill(conn)
        conn.commit()
        _log.info("Migration 47: error_events.group_key added + backfilled")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 47 (group_key) skipped: %s", e)


def _backfill(conn: sqlite3.Connection) -> None:
    """Recompute group_key for every row whose group_key IS NULL (single pass)."""
    from selfheal_grouping import group_key as _gk

    rows = conn.execute(
        "SELECT id, error_class, exc_type, entrypoint, traceback "
        "FROM error_events WHERE group_key IS NULL"
    ).fetchall()
    for r in rows:
        gk = _gk({
            "error_class": r["error_class"],
            "exc_type": r["exc_type"],
            "entrypoint": r["entrypoint"],
            "traceback": r["traceback"],
        })
        conn.execute("UPDATE error_events SET group_key=? WHERE id=?", (gk, r["id"]))
