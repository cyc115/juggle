"""dbops.migrations_recent — schema migrations 20-35.

Owns: the second half of the incremental migration chain (watchdog columns
onward), applied by ``dbops.migrations.run_migrations`` after migrations 1-19.
Must not own: query or business logic — only schema evolution.
"""

from __future__ import annotations

import logging
import sqlite3

from dbops.schema import (
    CREATE_AGENT_COMPLETIONS,
    CREATE_AGENT_TOOL_EVENTS,
    CREATE_ERROR_EVENTS,
    CREATE_GRAPH_EDGES,
    CREATE_GRAPH_NODES,
    CREATE_PROJECT_CORRECTIONS,
    CREATE_PROJECTS,
    CREATE_WATCHDOG_EVENTS,
    INBOX_PROJECT_ID,
    _now,
)

_log = logging.getLogger(__name__)


def apply_recent_migrations(conn: sqlite3.Connection) -> None:
    """Apply incremental schema migrations 20-35 (idempotent)."""
    # Migration 20: all watchdog columns on agents
    agents_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()
    }
    try:
        for col, defn in [
            ("watchdog_retried", "INTEGER NOT NULL DEFAULT 0"),
            ("watchdog_threshold_minutes", "INTEGER"),
            ("model", "TEXT"),
            ("last_task", "TEXT"),
            ("busy_since", "TEXT"),
            ("last_send_task_pane_hash", "TEXT"),
            ("last_send_task_at", "TEXT"),
            ("last_activity_at", "TEXT"),
        ]:
            if col not in agents_cols:
                conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {defn}")
        conn.commit()
        _log.info("Migration 20: watchdog columns added to agents")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 20 (watchdog agent cols) skipped: %s", e)

    # Migration 21: agent_completions table + index
    try:
        conn.execute(CREATE_AGENT_COMPLETIONS)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_completions_role_date "
            "ON agent_completions(role, completed_at)"
        )
        conn.commit()
        _log.info("Migration 21: agent_completions table created")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 21 (agent_completions) skipped: %s", e)

    # Migration 22: watchdog_events table + threads dispatch payload columns
    try:
        conn.execute(CREATE_WATCHDOG_EVENTS)
        threads_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()
        }
        for col in (
            "last_dispatched_task",
            "last_dispatched_role",
            "last_dispatched_model",
        ):
            if col not in threads_cols:
                conn.execute(f"ALTER TABLE threads ADD COLUMN {col} TEXT")
        conn.commit()
        _log.info(
            "Migration 22: watchdog_events + threads dispatch payload columns created"
        )
    except sqlite3.OperationalError as e:
        _log.warning("Migration 22 (watchdog_events + threads) skipped: %s", e)

    # Migration 23: last_reflect_msg_count for reflect() gate
    threads_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "last_reflect_msg_count" not in threads_cols:
        try:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN last_reflect_msg_count INTEGER DEFAULT 0"
            )
            conn.commit()
            _log.info("Migration 23: last_reflect_msg_count column added")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 23 (last_reflect_msg_count) skipped: %s", e)

    # Migration 24: error_events for self-heal
    tables_now = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "error_events" not in tables_now:
        try:
            conn.execute(CREATE_ERROR_EVENTS)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_error_events_sig "
                "ON error_events(signature_hash) WHERE status != 'resolved'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_error_events_status "
                "ON error_events(status)"
            )
            conn.commit()
            _log.info("Migration 24: error_events table created")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 24 (error_events) skipped: %s", e)

    # Migration 25: agent_tool_events telemetry table
    if "agent_tool_events" not in tables_now:
        try:
            conn.execute(CREATE_AGENT_TOOL_EVENTS)
            conn.commit()
            _log.info("Migration 25: agent_tool_events table created")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 25 (agent_tool_events) skipped: %s", e)

    # Migration 26: projects table + INBOX seed + project_id on threads
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    cols_threads = {
        r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "projects" not in tables:
        conn.execute(CREATE_PROJECTS)
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO projects (id, name, objective, status, created_at, last_active) VALUES (?,?,?,?,?,?)",
        (INBOX_PROJECT_ID, "Inbox", "Catch-all for unassigned threads", "active", now, now),
    )
    if "project_id" not in cols_threads:
        conn.execute(
            "ALTER TABLE threads ADD COLUMN project_id TEXT DEFAULT 'INBOX' REFERENCES projects(id)"
        )
        conn.execute("UPDATE threads SET project_id = 'INBOX' WHERE project_id IS NULL")

    # Migration 27: assigned_by on threads ('auto'|'human')
    cols_threads = {
        r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "assigned_by" not in cols_threads:
        try:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN assigned_by TEXT NOT NULL DEFAULT 'auto'"
            )
            conn.commit()
            _log.info("Migration 27: assigned_by column added to threads")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 27 (assigned_by) skipped: %s", e)

    # Migration 28: project_corrections append-only log
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "project_corrections" not in tables:
        try:
            conn.execute(CREATE_PROJECT_CORRECTIONS)
            conn.commit()
            _log.info("Migration 28: project_corrections table created")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 28 (project_corrections) skipped: %s", e)

    # Migration 29: summary + closed_at on projects (project close/open feature)
    proj_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()
    }
    try:
        if "summary" not in proj_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN summary TEXT DEFAULT ''")
        if "closed_at" not in proj_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN closed_at TEXT")
        conn.commit()
        _log.info("Migration 29: summary + closed_at added to projects")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 29 (projects summary/closed_at) skipped: %s", e)

    # Migration 30: match_profile + profile_synth_at + profile_dirty on projects
    proj_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()
    }
    try:
        if "match_profile" not in proj_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN match_profile TEXT DEFAULT ''")
        if "profile_synth_at" not in proj_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN profile_synth_at TEXT")
        if "profile_dirty" not in proj_cols:
            conn.execute(
                "ALTER TABLE projects ADD COLUMN profile_dirty INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()
        _log.info("Migration 30: match_profile columns added to projects")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 30 (match_profile) skipped: %s", e)

    # Migration 31: assigned_confidence on threads
    threads_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "assigned_confidence" not in threads_cols:
        try:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN assigned_confidence REAL DEFAULT NULL"
            )
            conn.commit()
            _log.info("Migration 31: assigned_confidence added to threads")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 31 (assigned_confidence) skipped: %s", e)

    # Migration 32: harness + oneshot_pid on agents
    agents_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()
    }
    try:
        for col, defn in [
            ("harness", "TEXT"),
            ("oneshot_pid", "INTEGER"),
        ]:
            if col not in agents_cols:
                conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {defn}")
        conn.commit()
        _log.info("Migration 32: harness + oneshot_pid columns added to agents")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 32 (harness/oneshot_pid) skipped: %s", e)

    # Migration 33: repo_path on agents
    agents_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()
    }
    if "repo_path" not in agents_cols:
        try:
            conn.execute("ALTER TABLE agents ADD COLUMN repo_path TEXT")
            conn.commit()
            _log.info("Migration 33: repo_path column added to agents")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 33 (repo_path) skipped: %s", e)

    # Migration 34: worktree columns on threads
    threads_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    try:
        for col, defn in [
            ("worktree_path", "TEXT"),
            ("worktree_branch", "TEXT"),
            ("main_repo_path", "TEXT"),
        ]:
            if col not in threads_cols:
                conn.execute(f"ALTER TABLE threads ADD COLUMN {col} {defn}")
        conn.commit()
        _log.info("Migration 34: worktree columns added to threads")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 34 (worktree) skipped: %s", e)

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
