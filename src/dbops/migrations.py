"""dbops.migrations — Incremental SQLite schema migration runner.

Owns: ``run_migrations(conn)`` — migrations 1-19 inline, then delegates to
``dbops.migrations_recent.apply_recent_migrations`` for 20-35. Each migration is idempotent (uses ALTER TABLE IF NOT EXISTS / INSERT
OR IGNORE patterns). Called by ``JuggleDB.init_db`` after table creation.
Must not own: any query or business logic — only schema evolution.
"""

from __future__ import annotations

import logging
import sqlite3

from dbops.migrations_recent import apply_recent_migrations
from dbops.schema import (
    CREATE_AGENTS,
)

_log = logging.getLogger(__name__)


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply incremental schema migrations 1-35 (20+ live in migrations_recent)."""
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

    # Migration 4 (new DBs): add label if missing.
    # Guard: once user_label exists (Migration 14+) the label column is dead —
    # skip to prevent the M4→M16 oscillation that fires _next_excel_label.
    if "label" not in cols and "id" in cols and "user_label" not in cols:
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

    # Migration 16: drop the dead 'label' column (no backfill).
    # The _next_excel_label backfill that was here raised 'All 702 user labels
    # in use' once 702 persisted labels existed.  The slug-wheel allocates
    # user_labels at create_thread time; legacy backfill is obsolete.
    # M4 guard (user_label not in cols) prevents this from re-running on
    # every init_db call, but M16 itself is idempotent: if 'label' somehow
    # appears again, just drop it.
    cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "label" in cols:
        try:
            conn.execute("ALTER TABLE threads DROP COLUMN label")
            _log.info("Migration 16: dropped dead label column")
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

    apply_recent_migrations(conn)
