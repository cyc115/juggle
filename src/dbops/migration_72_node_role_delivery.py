"""Migration 72 (loop-entity V1, Phase 1) — additive per-node role/delivery and
per-project kind columns.

Loop entity needs three discriminators, every one defaulting to CURRENT
behavior so existing graphs/projects are byte-for-byte unchanged:
  - nodes.role     TEXT DEFAULT 'coder'   — dispatch role (Phase 2 reads it;
                                            legacy nodes still dispatch coder).
  - nodes.delivery TEXT DEFAULT 'merge'   — completion contract (Phase 3 branches
                                            on it; 'merge' → verified as today).
  - projects.kind  TEXT DEFAULT 'project' — 'loop' projects self-schedule and are
                                            excluded from P-slots (Phase 4).

ADDITIVE only, idempotent, presence-guarded, fail-soft (matches Migration 70/71).
Never rebuilds the table. Reserved via `juggle migration next` (2026-07-04).
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_72_node_role_delivery(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    try:
        if "nodes" in tables:
            ncols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
            if "role" not in ncols:
                conn.execute(
                    "ALTER TABLE nodes ADD COLUMN role TEXT NOT NULL DEFAULT 'coder'")
            if "delivery" not in ncols:
                conn.execute(
                    "ALTER TABLE nodes ADD COLUMN delivery TEXT NOT NULL DEFAULT 'merge'")
        if "projects" in tables:
            pcols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
            if "kind" not in pcols:
                conn.execute(
                    "ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'project'")
        conn.commit()
        _log.info("Migration 72: nodes.role/delivery + projects.kind ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 72 (node_role_delivery) skipped: %s", e)
