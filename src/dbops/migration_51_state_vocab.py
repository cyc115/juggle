"""Migration 51 (P8 C3 + R2-4): unify the task-entry vocab to 'open' — FAIL-LOUD.

Rewrites the legacy 'pending' task/topic state to 'open' in graph_tasks,
graph_topics, and the mirrored task nodes, so the unified node_transition
engine (which only models 'open') never meets a 'pending' row. Idempotent
(WHERE state='pending' -> second run no-ops); value-only (no schema change).

FAIL-LOUD (R2-4): the SAME-RELEASE engine rename (Tasks 1.3+1.4) hard-depends on
this migration having applied. A fail-soft swallow would silently strand
'pending' rows the renamed engine can neither see (ready_eligible) nor transition
(node_transition has no 'pending' entry) -> tasks stall with no error. So we take
the write lock up front with BEGIN IMMEDIATE (exactly like Migration 45,
migration_selfheal_status_check.py) and let lock contention PROPAGATE; the
init_db caller aborts the upgrade on the raise. Apply via juggle doctor (behind
assert_migration_allowed); never run directly against the shared prod DB.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_51_state_vocab(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    # Pre-Migration-44 DB without any target table -> return BEFORE taking the
    # lock so a brand-new DB upgrade is a cheap no-op (not a spurious lock grab).
    if not ({"graph_tasks", "graph_topics", "nodes"} & tables):
        return
    prev_isolation = conn.isolation_level
    conn.isolation_level = None              # explicit transaction control
    conn.execute("BEGIN IMMEDIATE")          # write lock up front; raises on contention (fail-LOUD)
    try:
        if "graph_tasks" in tables:
            conn.execute("UPDATE graph_tasks SET state='open' WHERE state='pending'")
        if "graph_topics" in tables:
            conn.execute("UPDATE graph_topics SET state='open' WHERE state='pending'")
        if "nodes" in tables:
            conn.execute(
                "UPDATE nodes SET state='open' WHERE kind='task' AND state='pending'")
        conn.execute("COMMIT")
        _log.info("Migration 51: task-state vocab unified pending->open")
    except Exception:
        conn.execute("ROLLBACK")             # fail-LOUD: abort the upgrade, do NOT swallow
        raise
    finally:
        conn.isolation_level = prev_isolation
