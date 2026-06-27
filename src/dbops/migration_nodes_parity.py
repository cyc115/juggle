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
    # P8 Q2: the task->agent-thread link (graph_*.thread_id) has no other nodes home.
    ("dispatch_thread_id", "ALTER TABLE nodes ADD COLUMN dispatch_thread_id TEXT"),
]


def migrate_50_nodes_parity(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    try:
        for name, ddl in _ADDS:
            if name not in cols:
                conn.execute(ddl)
        # Partial unique index scoped to LIVE conversation nodes only — mirrors the
        # legacy idx_threads_live_label (live states active/running/background ->
        # node states 'open'/'running'). Kind-scoped because nodes unions kinds; and
        # state-scoped because the slug wheel recycles a freed user_label to a new
        # live thread while archived threads keep it as a historical handle, so a
        # live + archived conversation node can legitimately share a slug (both
        # mirrored by Migration 44). An unscoped index would crash backfill on any
        # such prod DB (2026-06-22 recycled-slug IntegrityError).
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_user_label "
            "ON nodes(user_label) WHERE kind='conversation' AND user_label IS NOT NULL "
            "AND state IN ('open', 'running')")
        conn.commit()
        _log.info("Migration 50: nodes parity columns + slug index ensured")
    except sqlite3.OperationalError as e:   # fail-soft (additive convention)
        _log.warning("Migration 50 (nodes parity) skipped: %s", e)


def backfill_nodes_parity(conn: sqlite3.Connection) -> None:
    """Copy parity columns from threads into the id-matched conversation nodes.
    Idempotent: re-running is a no-op on already-synced rows. Fixes Migration-44
    staleness (it read threads.last_active, not last_active_at)."""
    backfill_graph_parity(conn)             # P8 Q2/Q3 — always runs (graph-table gated)
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


def backfill_graph_parity(conn: sqlite3.Connection) -> None:
    """P8 Q2 graph-tier backfill (id-anchored, idempotent):

      * dispatch_thread_id ← graph_tasks.thread_id (task-tier nodes) and the real
        graph_topics.thread_id (is_mirror=0, topic-tier nodes). Replaces the
        graph_*.thread_id link that has no other nodes home.

    Note (P8 C3, 2026-06-27): the former open->pending state correction is
    DELETED. The task-entry vocab is now unified to open (db_graph queries
    state=open), so re-introducing the legacy state would strand the node from
    the renamed engine. Migration 51 rewrites any residual legacy state to open.

    Runs only while the legacy graph tables are still present (pre-drop).
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "nodes" not in tables:
        return
    if "dispatch_thread_id" not in {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}:
        return
    try:
        if "graph_tasks" in tables:
            conn.execute("""
                UPDATE nodes SET dispatch_thread_id =
                  (SELECT g.thread_id FROM graph_tasks g WHERE g.id=nodes.id)
                WHERE kind='task' AND parent_id IS NOT NULL
                  AND EXISTS (SELECT 1 FROM graph_tasks g WHERE g.id=nodes.id AND g.thread_id IS NOT NULL)
            """)
        if "graph_topics" in tables:
            conn.execute("""
                UPDATE nodes SET dispatch_thread_id =
                  (SELECT g.thread_id FROM graph_topics g WHERE g.id=nodes.id AND COALESCE(g.is_mirror,0)=0)
                WHERE kind='task' AND parent_id IS NULL
                  AND EXISTS (SELECT 1 FROM graph_topics g
                              WHERE g.id=nodes.id AND COALESCE(g.is_mirror,0)=0 AND g.thread_id IS NOT NULL)
            """)
        conn.commit()
        _log.info("Migration 50 graph backfill: dispatch_thread_id populated")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 50 graph backfill skipped: %s", e)
