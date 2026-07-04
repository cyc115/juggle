"""Migration 73 (loop-entity V1, Phase 1) — additive ``loops`` table.

Installs the loop entity (recurring, self-scheduling project driver) plus its
run-seq id-namespace counter and the Phase-5 circuit-breaker columns (reserved
here per plan open-question #2 so Phase 5 adds no migration). DDL lives in
dbops.schema_loops.CREATE_LOOPS so fresh `db init` and this migrate path stay in
lockstep.

ADDITIVE only, idempotent (CREATE TABLE IF NOT EXISTS), fail-soft (matches
Migration 70/71). Never rebuilds the table. Reserved via `juggle migration next`
(2026-07-04).
"""
from __future__ import annotations

import logging
import sqlite3

from dbops.schema_loops import CREATE_LOOPS, CREATE_LOOPS_INDEXES

_log = logging.getLogger(__name__)


def migrate_73_loops(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(CREATE_LOOPS)
        for idx in CREATE_LOOPS_INDEXES:
            conn.execute(idx)
        conn.commit()
        _log.info("Migration 73: loops table ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 73 (loops) skipped: %s", e)
