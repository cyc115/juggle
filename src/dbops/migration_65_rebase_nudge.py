"""Migration 65 (P3, irl-prevention) — additive ``nodes.rebase_nudge_sent``.

Single-nudge-only tracking for the early-rebase advisory (P3): a topic gets
at most one "trunk moved ≥5 commits" nudge per running episode. Reset to 0
when a topic (re-)enters 'running' (fresh dispatch); set to 1 once the nudge
fires. ADDITIVE only, idempotent, presence-guarded, fail-soft.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_65_rebase_nudge(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "nodes" not in tables:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "rebase_nudge_sent" in cols:
        return
    try:
        conn.execute(
            "ALTER TABLE nodes ADD COLUMN rebase_nudge_sent INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
        _log.info("Migration 65: nodes.rebase_nudge_sent column ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 65 (rebase_nudge_sent) skipped: %s", e)
