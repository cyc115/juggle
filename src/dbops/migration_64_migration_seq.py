"""Migration 64 (P2, irl-prevention) — bootstrap the ``migration_seq`` table.

Plain hand-picked number (chicken-egg: the allocator can't allocate its own
first number). Delegates to ``dbops.migration_seq.ensure_migration_seq`` so
the table-creation logic has one source of truth with the CLI's reservation
path — additive, idempotent, fail-soft.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_64_migration_seq(conn: sqlite3.Connection) -> None:
    from dbops.migration_seq import ensure_migration_seq
    try:
        ensure_migration_seq(conn)
        _log.info("Migration 64: migration_seq table ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 64 (migration_seq) skipped: %s", e)
