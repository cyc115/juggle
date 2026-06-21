"""Migration 46 — persistent topic-summary cache (cockpit `i` modal).

Additive, idempotent `CREATE TABLE IF NOT EXISTS` (no table rebuild → no
watchdog-quiesce concern). One row per thread; the durable L2 store behind the
modal's in-memory L1 dict. Persist-only v1 — the incremental path (base cursor /
incremental counter) is deferred, so those columns are intentionally absent.
"""

from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger("juggle.migrations")

CREATE_TOPIC_SUMMARY_CACHE = """
CREATE TABLE IF NOT EXISTS topic_summary_cache (
  thread_id        TEXT PRIMARY KEY,
  last_message_id  INTEGER NOT NULL,
  summary_json     TEXT NOT NULL,
  generated_at     TEXT NOT NULL
);
"""


def migrate_46_topic_summary_cache(conn: sqlite3.Connection) -> None:
    """Create the topic_summary_cache table. Idempotent; safe on fresh + existing DBs."""
    try:
        conn.execute(CREATE_TOPIC_SUMMARY_CACHE)
        conn.commit()
    except sqlite3.OperationalError as e:  # pragma: no cover - defensive
        _log.warning("Migration 46 (topic_summary_cache) skipped: %s", e)
