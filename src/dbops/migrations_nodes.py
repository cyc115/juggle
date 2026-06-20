"""dbops.migrations_nodes — Migration 44: unified nodes + node_edges tables (P1).

Owns: additive schema creation + forward data backfill for the unified
topic-graph refactor (spec: specs/2026-06-18-unified-topic-graph.md §8).

Rules:
- OLD tables (threads, graph_topics, graph_tasks, graph_edges) are NEVER
  touched — this migration is purely additive.
- Idempotent: INSERT OR IGNORE guards against re-runs duplicating rows.
- Only run via juggle doctor against temp/prod DB; never called directly.
"""
from __future__ import annotations

import logging
import sqlite3

from dbops.schema_nodes import CREATE_NODE_EDGES, CREATE_NODES, CREATE_NODES_INDEXES

_log = logging.getLogger(__name__)

# threads.status → node.state (§4.3 of the spec)
_THREAD_STATUS_MAP = {
    "active": "open",
    "background": "running",
    "running": "running",
    "closed": "done",
    "failed": "failed-exec",
    "done": "done",
    "archived": "archived",
}

# pending is a legacy state in graph_topics/graph_tasks; map to 'open'
def _task_state(state: str) -> str:
    return "open" if state == "pending" else state


def apply_nodes_migration(conn: sqlite3.Connection) -> None:
    """Migration 44: create nodes + node_edges and backfill from old tables.

    Steps (single transaction):
      1. Create nodes + node_edges tables (idempotent DDL).
      2. Backfill threads → nodes (kind='conversation').
      3. Backfill graph_topics(is_mirror=0) → nodes (kind='task').
      4. Backfill graph_topics(is_mirror=1) → nodes (kind='conversation').
      5. Backfill graph_tasks → nodes (kind='task').
      6. Backfill graph_edges → node_edges.
    """
    try:
        # ── Step 1: Create tables ────────────────────────────────────────────
        conn.execute(CREATE_NODES)
        conn.execute(CREATE_NODE_EDGES)
        for idx in CREATE_NODES_INDEXES:
            conn.execute(idx)
        conn.commit()
        _log.info("Migration 44: nodes + node_edges tables created")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 44 (create tables) skipped: %s", e)
        return

    try:
        _backfill_threads(conn)
        _backfill_topics_task(conn)
        _backfill_topics_conversation(conn)
        _backfill_graph_tasks(conn)
        _backfill_node_edges(conn)
        conn.commit()
        _log.info("Migration 44: backfill complete")
    except sqlite3.OperationalError as e:
        conn.rollback()
        _log.warning("Migration 44 (backfill) failed: %s", e)


def _backfill_threads(conn: sqlite3.Connection) -> None:
    """Step 2: threads → nodes (kind='conversation')."""
    rows = conn.execute("""
        SELECT id, topic, status, session_id, summary, key_decisions,
               open_questions, last_user_intent, agent_task_id, agent_result,
               show_in_list, summarized_msg_count,
               last_dispatched_task, last_dispatched_role, last_dispatched_model,
               worktree_path, worktree_branch, main_repo_path,
               created_at, last_active
        FROM threads
    """).fetchall()

    for r in rows:
        state = _THREAD_STATUS_MAP.get(r["status"], "open")
        conn.execute("""
            INSERT OR IGNORE INTO nodes (
                id, kind, title, objective, state,
                project_id, parent_id,
                worktree_path, worktree_branch, main_repo_path,
                agent_task_id, agent_result,
                last_dispatched_task, last_dispatched_role, last_dispatched_model,
                session_id, summary, key_decisions, open_questions,
                last_user_intent, summarized_msg_count, show_in_list,
                created_at, updated_at
            ) VALUES (
                ?, 'conversation', ?, ?, ?,
                NULL, NULL,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?
            )
        """, (
            r["id"], r["topic"], r["last_user_intent"] or "", state,
            r["worktree_path"], r["worktree_branch"], r["main_repo_path"],
            r["agent_task_id"], r["agent_result"],
            r["last_dispatched_task"], r["last_dispatched_role"], r["last_dispatched_model"],
            r["session_id"], r["summary"], r["key_decisions"], r["open_questions"],
            r["last_user_intent"] or "", r["summarized_msg_count"], r["show_in_list"],
            r["created_at"], r["last_active"],
        ))


def _backfill_topics_task(conn: sqlite3.Connection) -> None:
    """Step 3: graph_topics(is_mirror=0) → nodes (kind='task', topic-tier)."""
    rows = conn.execute("""
        SELECT id, project_id, title, objective, state, handoff,
               diffstat, verified_at, merged_sha, created_at, updated_at
        FROM graph_topics
        WHERE is_mirror = 0
    """).fetchall()

    for r in rows:
        conn.execute("""
            INSERT OR IGNORE INTO nodes (
                id, kind, title, objective, state,
                project_id, parent_id,
                handoff, diffstat, verified_at, merged_sha,
                show_in_list, summarized_msg_count,
                created_at, updated_at
            ) VALUES (
                ?, 'task', ?, ?, ?,
                ?, NULL,
                ?, ?, ?, ?,
                1, 0,
                ?, ?
            )
        """, (
            r["id"], r["title"], r["objective"] or "", _task_state(r["state"]),
            r["project_id"],
            r["handoff"], r["diffstat"], r["verified_at"], r["merged_sha"],
            r["created_at"], r["updated_at"],
        ))


def _backfill_topics_conversation(conn: sqlite3.Connection) -> None:
    """Step 4: graph_topics(is_mirror=1) → nodes (kind='conversation')."""
    rows = conn.execute("""
        SELECT id, project_id, title, objective, state,
               created_at, updated_at
        FROM graph_topics
        WHERE is_mirror = 1
    """).fetchall()

    for r in rows:
        state = _task_state(r["state"])
        conn.execute("""
            INSERT OR IGNORE INTO nodes (
                id, kind, title, objective, state,
                project_id, parent_id,
                show_in_list, summarized_msg_count,
                created_at, updated_at
            ) VALUES (
                ?, 'conversation', ?, ?, ?,
                ?, NULL,
                1, 0,
                ?, ?
            )
        """, (
            r["id"], r["title"], r["objective"] or "", state,
            r["project_id"],
            r["created_at"], r["updated_at"],
        ))


def _backfill_graph_tasks(conn: sqlite3.Connection) -> None:
    """Step 5: graph_tasks → nodes (kind='task', task-tier sub-nodes)."""
    rows = conn.execute("""
        SELECT id, project_id, title, prompt, verify_cmd, state,
               topic_id, handoff, diffstat, verified_at,
               created_at, updated_at
        FROM graph_tasks
    """).fetchall()

    for r in rows:
        conn.execute("""
            INSERT OR IGNORE INTO nodes (
                id, kind, title, objective, state,
                project_id, parent_id,
                verify_cmd,
                handoff, diffstat, verified_at,
                show_in_list, summarized_msg_count,
                created_at, updated_at
            ) VALUES (
                ?, 'task', ?, ?, ?,
                ?, ?,
                ?,
                ?, ?, ?,
                1, 0,
                ?, ?
            )
        """, (
            r["id"], r["title"], r["prompt"] or "", _task_state(r["state"]),
            r["project_id"], r["topic_id"],
            r["verify_cmd"],
            r["handoff"], r["diffstat"], r["verified_at"],
            r["created_at"], r["updated_at"],
        ))


def _backfill_node_edges(conn: sqlite3.Connection) -> None:
    """Step 6: graph_edges.(task_id, depends_on_id) → node_edges."""
    rows = conn.execute(
        "SELECT task_id, depends_on_id FROM graph_edges"
    ).fetchall()
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO node_edges (node_id, depends_on_id) VALUES (?, ?)",
            (r["task_id"], r["depends_on_id"]),
        )
