"""juggle_db_migrations — Incremental SQLite schema migration runner.

Owns: the single ``run_migrations(conn)`` function that applies migrations 1-34
in order. Each migration is idempotent (uses ALTER TABLE IF NOT EXISTS / INSERT
OR IGNORE patterns). Called by ``JuggleDB.init_db`` after table creation.
Must not own: any query or business logic — only schema evolution.
"""

from __future__ import annotations

import logging
import sqlite3

from juggle_db_schema import (
    CREATE_AGENT_COMPLETIONS,
    CREATE_AGENT_TOOL_EVENTS,
    CREATE_AGENTS,
    CREATE_ERROR_EVENTS,
    CREATE_PROJECT_CORRECTIONS,
    CREATE_PROJECTS,
    CREATE_WATCHDOG_EVENTS,
    INBOX_PROJECT_ID,
    _next_excel_label,
    _now,
)

_log = logging.getLogger(__name__)


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply incremental schema migrations 1-34."""
    cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()
    }

    # Migration 1: thread_id → id + label
    if "thread_id" in cols and "id" not in cols:
        conn.execute("ALTER TABLE threads RENAME TO threads_old")
        conn.execute("""
            CREATE TABLE threads (
              id              TEXT PRIMARY KEY,
              label           TEXT,
              session_id      TEXT NOT NULL DEFAULT '',
              topic           TEXT NOT NULL,
              status          TEXT NOT NULL DEFAULT 'active',
              summary         TEXT DEFAULT '',
              key_decisions   TEXT DEFAULT '[]',
              open_questions  TEXT DEFAULT '[]',
              last_user_intent TEXT DEFAULT '',
              agent_task_id   TEXT,
              agent_result    TEXT,
              show_in_list    INTEGER NOT NULL DEFAULT 1,
              summarized_msg_count INTEGER NOT NULL DEFAULT 0,
              created_at      TEXT NOT NULL,
              last_active     TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO threads (id, label, session_id, topic, status, summary,
              key_decisions, open_questions, last_user_intent, agent_task_id, agent_result,
              show_in_list, summarized_msg_count, created_at, last_active)
            SELECT thread_id, thread_id, session_id, topic, status, summary,
              key_decisions, open_questions, last_user_intent, agent_task_id, agent_result,
              COALESCE(show_in_list, 1), COALESCE(summarized_msg_count, 0),
              created_at, last_active
            FROM threads_old
        """)
        conn.execute("DROP TABLE threads_old")
        return  # remaining migrations don't apply to legacy schema

    # Migration 2 (new DBs): add summarized_msg_count if missing
    if "summarized_msg_count" not in cols:
        try:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN summarized_msg_count INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError as e:
            _log.warning("Migration 2 skipped: %s", e)

    # Migration 3 (new DBs): add show_in_list if missing
    if "show_in_list" not in cols:
        try:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN show_in_list INTEGER NOT NULL DEFAULT 1"
            )
        except sqlite3.OperationalError as e:
            _log.warning("Migration 3 skipped: %s", e)

    # Migration 4 (new DBs): add label if missing
    if "label" not in cols and "id" in cols:
        try:
            conn.execute("ALTER TABLE threads ADD COLUMN label TEXT")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 4 skipped: %s", e)

    # Migration 5: add title column for LLM-generated short titles
    if "title" not in cols:
        try:
            conn.execute("ALTER TABLE threads ADD COLUMN title TEXT DEFAULT ''")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 5 skipped: %s", e)

    # Migration 6: add agents table for tmux persistent agent pool
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "agents" not in tables:
        conn.execute(CREATE_AGENTS)

    # Migration 7: add domain to threads
    cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "domain" not in cols:
        try:
            conn.execute("ALTER TABLE threads ADD COLUMN domain TEXT DEFAULT NULL")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 7 skipped: %s", e)

    # Migration 8: add domain to agents
    agent_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(agents)").fetchall()
    }
    if "domain" not in agent_cols:
        try:
            conn.execute("ALTER TABLE agents ADD COLUMN domain TEXT DEFAULT NULL")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 8 skipped: %s", e)

    # Migration 9: (removed in 1.21.0) — previously seeded domains/domain_paths
    # tables, which are now dropped in Migrations 17–19. Body intentionally empty.

    # Migration 10: add memory columns for Hindsight integration
    cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "memory_loaded" not in cols:
        try:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN memory_context TEXT DEFAULT ''"
            )
            conn.execute(
                "ALTER TABLE threads ADD COLUMN memory_loaded INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError as e:
            _log.warning("Migration 10 skipped: %s", e)

    # Migration 11: add delivery_attempts to notifications for escalation
    notif_cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(notifications)").fetchall()
    }
    if "delivery_attempts" not in notif_cols:
        try:
            conn.execute(
                "ALTER TABLE notifications ADD COLUMN delivery_attempts INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError as e:
            _log.warning("Migration 11 skipped: %s", e)

    # Migration 12: add reviewed flag for cockpit REVIEW nudge
    cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "reviewed" not in cols:
        try:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN reviewed INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError as e:
            _log.warning("Migration 12 skipped: %s", e)

    # Migration 13: add severity column for cockpit notification routing
    notif_cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(notifications)").fetchall()
    }
    if "severity" not in notif_cols:
        try:
            conn.execute(
                "ALTER TABLE notifications ADD COLUMN severity TEXT DEFAULT 'action'"
            )
        except sqlite3.OperationalError as e:
            _log.warning("Migration 13 skipped: %s", e)

    # Migration 14: add user_label + last_active_at to threads
    cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "user_label" not in cols:
        try:
            conn.execute("ALTER TABLE threads ADD COLUMN user_label TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_user_label "
                "ON threads(user_label) WHERE user_label IS NOT NULL"
            )
        except sqlite3.OperationalError as e:
            _log.warning("Migration 14 (user_label) skipped: %s", e)
    if "last_active_at" not in cols:
        try:
            conn.execute("ALTER TABLE threads ADD COLUMN last_active_at TEXT")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 14 (last_active_at) skipped: %s", e)

    # Migration 15: seed thread_auto_archive_ttl_secs setting
    try:
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES "
            "('thread_auto_archive_ttl_secs', '3600')"
        )
    except sqlite3.OperationalError as e:
        _log.warning("Migration 15 (settings seed) skipped: %s", e)

    # Migration 16: backfill user_label for legacy threads, then drop label column
    cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "label" in cols:
        try:
            used = {
                row["user_label"]
                for row in conn.execute(
                    "SELECT user_label FROM threads WHERE user_label IS NOT NULL"
                ).fetchall()
            }
            missing = conn.execute(
                "SELECT id FROM threads WHERE user_label IS NULL"
            ).fetchall()
            for row in missing:
                ul = _next_excel_label(used)
                conn.execute(
                    "UPDATE threads SET user_label = ? WHERE id = ?",
                    (ul, row["id"]),
                )
                used.add(ul)
            conn.execute("ALTER TABLE threads DROP COLUMN label")
            _log.info(
                "Migration 16: backfilled %d threads, dropped label column",
                len(missing),
            )
        except sqlite3.OperationalError as e:
            _log.warning("Migration 16 skipped: %s", e)

    # Migration 17: drop domain column from threads
    cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "domain" in cols:
        try:
            domain_indexes = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='threads' AND sql LIKE '%domain%'"
                ).fetchall()
            ]
            for idx_name in domain_indexes:
                safe = idx_name.replace('"', '""')
                conn.execute(f'DROP INDEX IF EXISTS "{safe}"')
            conn.execute("ALTER TABLE threads DROP COLUMN domain")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 17 skipped: %s", e)

    # Migration 18: drop domain column from agents
    agent_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(agents)").fetchall()
    }
    if "domain" in agent_cols:
        try:
            conn.execute("ALTER TABLE agents DROP COLUMN domain")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 18 skipped: %s", e)

    # Migration 19: drop domain tables (domain_paths FK → domains, drop in order)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "domain_paths" in tables or "domains" in tables:
        try:
            conn.execute("DROP TABLE IF EXISTS domain_paths")
            conn.execute("DROP TABLE IF EXISTS domains")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 19 skipped: %s", e)

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
