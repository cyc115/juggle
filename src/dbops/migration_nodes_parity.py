"""Migration 50 (unified-topic-graph P8 prep): additive nodes parity columns +
kind-scoped slug-uniqueness index. Idempotent; ADDITIVE only (no rebuild).

DO NOT run against the shared production DB directly; apply via juggle doctor.
"""
from __future__ import annotations
import logging
import sqlite3

_log = logging.getLogger(__name__)

_ADDS = [
    ("user_label", "ALTER TABLE nodes ADD COLUMN user_label TEXT"),
    ("assigned_by", "ALTER TABLE nodes ADD COLUMN assigned_by TEXT NOT NULL DEFAULT 'auto'"),
    ("last_active_at", "ALTER TABLE nodes ADD COLUMN last_active_at TEXT"),
]


def migrate_50_nodes_parity(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    try:
        for name, ddl in _ADDS:
            if name not in cols:
                conn.execute(ddl)
        # kind-scoped partial unique index: only live CONVERSATION nodes share the
        # slug wheel (nodes unions kinds, so an unscoped unique index is wrong).
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_user_label "
            "ON nodes(user_label) WHERE kind='conversation' AND user_label IS NOT NULL")
        conn.commit()
        _log.info("Migration 50: nodes parity columns + slug index ensured")
    except sqlite3.OperationalError as e:   # fail-soft (additive convention)
        _log.warning("Migration 50 (nodes parity) skipped: %s", e)


def backfill_nodes_parity(conn: sqlite3.Connection) -> None:
    """Copy parity columns from threads into the id-matched conversation nodes.
    Idempotent: re-running is a no-op on already-synced rows. Fixes Migration-44
    staleness (it read threads.last_active, not last_active_at)."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "threads" not in tables or "nodes" not in tables:
        return
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
    if "user_label" not in tcols:           # nothing to backfill from
        return
    la = "COALESCE(t.last_active_at, t.last_active)" if "last_active_at" in tcols else "t.last_active"
    try:
        conn.execute(f"""
            UPDATE nodes SET
              user_label    = (SELECT t.user_label FROM threads t WHERE t.id=nodes.id),
              assigned_by   = COALESCE((SELECT t.assigned_by FROM threads t WHERE t.id=nodes.id), 'auto'),
              last_active_at= (SELECT {la} FROM threads t WHERE t.id=nodes.id),
              updated_at    = COALESCE((SELECT {la} FROM threads t WHERE t.id=nodes.id), updated_at)
            WHERE kind='conversation' AND EXISTS (SELECT 1 FROM threads t WHERE t.id=nodes.id)
        """)
        conn.commit()
        _log.info("Migration 50 backfill: nodes parity columns populated from threads")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 50 backfill skipped: %s", e)
