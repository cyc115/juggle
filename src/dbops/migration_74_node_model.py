"""Migration 74 (loop-entity V2, Release 1 / P2) — additive per-node requested
``model`` column.

Persists the model a node/topic wants dispatched with, at TOPIC grain (tasks
inherit their parent topic's model via ``parent_id``). DEFAULT NULL preserves
today's behavior exactly (NULL = harness default / role-resolved). This is the
REQUESTED-input model — distinct from the pre-existing
``nodes.last_dispatched_model`` (dispatch telemetry: what was last dispatched).
Do not conflate the two.

Like Migrations 70/71/72 this column is migration-only and deliberately NOT
carried in the frozen ``CREATE_NODES`` DDL (frozen at Migration 63): a fresh DB
acquires it via the SAME ALTER-append path as an already-migrated DB, so
``SELECT *`` column order stays provenance-identical between the two. See
``schema_nodes.py`` for the frozen-DDL rationale.

ADDITIVE only, idempotent, presence-guarded, fail-soft (matches Migration
70/71/72). Never rebuilds the table. Reserved via `juggle migration next`
(2026-07-04).
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_74_node_model(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    try:
        if "nodes" in tables:
            ncols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
            if "model" not in ncols:
                conn.execute("ALTER TABLE nodes ADD COLUMN model TEXT DEFAULT NULL")
        conn.commit()
        _log.info("Migration 74: nodes.model ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 74 (node_model) skipped: %s", e)
