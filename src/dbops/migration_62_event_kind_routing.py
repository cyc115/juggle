"""Migration 62 (irl-backbone R1) — additive ``notifications_v2.kind`` /
``notifications_v2.handled_by`` columns.

Phase 0 of the event backbone: every notification will carry a ``kind``
(one of a fixed enum, wired in T1a) and a ``handled_by`` delivery-routing
tag (watchdog / orchestrator / user, wired in T1a). This migration only
adds the columns so existing rows and existing emitters keep working
unchanged — ``kind`` defaults to ``'legacy'`` and ``handled_by`` to ``''``
until T1a converts known emitters to real kinds.

ADDITIVE only, idempotent, presence-guarded, fail-soft (matches Migration 61).
Never rebuilds the table.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_62_event_kind_routing(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "notifications_v2" not in tables:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(notifications_v2)")}
    try:
        if "kind" not in cols:
            conn.execute(
                "ALTER TABLE notifications_v2 ADD COLUMN kind TEXT NOT NULL DEFAULT 'legacy'"
            )
        if "handled_by" not in cols:
            conn.execute(
                "ALTER TABLE notifications_v2 ADD COLUMN handled_by TEXT NOT NULL DEFAULT ''"
            )
        conn.commit()
        _log.info("Migration 62: notifications_v2.kind/handled_by columns ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 62 (event kind routing) skipped: %s", e)
