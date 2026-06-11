"""dbops.migrations_graph — autopilot graph/topic store migrations (35-37).

Owns: schema evolution for the project-autopilot plan store
(graph_nodes / graph_edges / graph_topics). Extracted from
dbops.migrations_recent to keep both modules within the 300-line architecture
gate. Called by ``dbops.migrations_recent.apply_recent_migrations``.
Must not own: query or business logic — only schema evolution.
"""

from __future__ import annotations

import logging
import sqlite3

from dbops.schema_graph import (
    CREATE_GRAPH_EDGES,
    CREATE_GRAPH_NODES,
    CREATE_GRAPH_TOPICS,
)

_log = logging.getLogger(__name__)


def apply_graph_migrations(conn: sqlite3.Connection) -> None:
    """Apply migrations 35-37 (graph_nodes / graph_edges / graph_topics)."""
    # Migration 35: graph_nodes + graph_edges plan store for project autopilot
    # (design 2026-06-10 rev 2 — nodes hold the plan, threads only execute)
    try:
        conn.execute(CREATE_GRAPH_NODES)
        conn.execute(CREATE_GRAPH_EDGES)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_project_state "
            "ON graph_nodes(project_id, state)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_thread "
            "ON graph_nodes(thread_id) WHERE thread_id IS NOT NULL"
        )
        conn.commit()
        _log.info("Migration 35: graph_nodes + graph_edges tables created")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 35 (graph_nodes/graph_edges) skipped: %s", e)

    # Migration 36: graph_nodes.diffstat — pre-merge diffstat captured inside
    # _run_integrate for dependent-node hydration (autopilot Phase 3, DA M4;
    # the branch+worktree are deleted on merge, so capture must be pre-merge)
    try:
        conn.execute("ALTER TABLE graph_nodes ADD COLUMN diffstat TEXT")
        conn.commit()
        _log.info("Migration 36: graph_nodes.diffstat column added")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 36 (graph_nodes.diffstat) skipped: %s", e)

    # Migration 37: graph_topics + graph_nodes.topic_id (3-tier R9, 2026-06-11).
    # Backfill wraps each flat node in a synthetic single-task topic ADOPTING
    # state/thread_id/updated_at — in-flight graphs keep running, and the
    # stale-sweep clock is preserved (updated_at copied, never now()).
    try:
        conn.execute(CREATE_GRAPH_TOPICS)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_topics_project_state "
            "ON graph_topics(project_id, state)"
        )
        try:
            conn.execute(
                "ALTER TABLE graph_nodes ADD COLUMN topic_id TEXT "
                "REFERENCES graph_topics(id)"
            )
        except sqlite3.OperationalError:
            pass  # column exists — idempotent re-run
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_topic "
            "ON graph_nodes(topic_id)"
        )
        node_ids = {r[0] for r in conn.execute("SELECT id FROM graph_nodes")}
        rows = conn.execute(
            "SELECT * FROM graph_nodes WHERE topic_id IS NULL"
        ).fetchall()
        for n in rows:
            tid = f"T-{n['id']}"
            if tid in node_ids:  # node literally named 'T-<x>' exists
                tid = f"T#{n['id']}"
            conn.execute(
                "INSERT OR IGNORE INTO graph_topics (id, project_id, title, "
                "objective, state, thread_id, handoff, diffstat, verified_at, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (tid, n["project_id"], n["title"], "", n["state"],
                 n["thread_id"], n["handoff"], n["diffstat"], n["verified_at"],
                 n["created_at"], n["updated_at"]),
            )
            conn.execute(
                "UPDATE graph_nodes SET topic_id=? WHERE id=?", (tid, n["id"])
            )
        conn.commit()
        _log.info("Migration 37: graph_topics created, %d node(s) backfilled",
                  len(rows))
    except sqlite3.OperationalError as e:
        _log.warning("Migration 37 (graph_topics) skipped: %s", e)
