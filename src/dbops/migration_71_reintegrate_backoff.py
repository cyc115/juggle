"""Migration 71 (fix-reintegrate-persist-backoff) — durable re-integrate backoff
state: additive ``nodes.reintegrate_attempts`` / ``reintegrate_last_attempt`` /
``reintegrate_pid`` columns.

Incident (2026-07-03/04 integrate-wedge #2, RC4 of 4): the watchdog re-integrate
driver kept its attempt counter, backoff timer, and single-flight proc handle in
an in-memory module dict. The watchdog restarts on every plugin-HEAD advance —
which every successful integrate causes — so the state was wiped constantly:
perpetual 'attempt 1', the spawn-failure bound never accumulated, and the lost
proc handle let a restart double-spawn a gate for the same topic. These columns
persist that state on the topic's node row so it survives a restart.

ADDITIVE only, idempotent, presence-guarded, fail-soft (matches Migration 70).
Never rebuilds the table.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_71_reintegrate_backoff(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "nodes" not in tables:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    try:
        if "reintegrate_attempts" not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN "
                         "reintegrate_attempts INTEGER NOT NULL DEFAULT 0")
        if "reintegrate_last_attempt" not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN reintegrate_last_attempt TEXT")
        if "reintegrate_pid" not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN reintegrate_pid INTEGER")
        conn.commit()
        _log.info("Migration 71: nodes.reintegrate_{attempts,last_attempt,pid} ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 71 (reintegrate_backoff) skipped: %s", e)
