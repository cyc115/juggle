#!/usr/bin/env python3
"""Juggle DB — SQLite state manager for the multi-topic conversation orchestrator.

Composition root: assembles JuggleDB from focused mixin modules and re-exports
all public names so every existing ``from juggle_db import X`` continues to
work without caller changes.

Domain modules (each ≤300 lines):
  dbops/schema.py        — DDL, module constants, pure helpers
  dbops/migrations.py    — incremental schema migration runner (run_migrations)
  dbops/session.py       — SessionMixin  (session KV, active flag, settings)
  dbops/threads.py       — ThreadsMixin  (thread CRUD, state machine, archive)
  dbops/projects.py      — ProjectsMixin (project CRUD, match-profile, corrections)
  dbops/messages.py      — MessagesMixin (message storage, context-window queries)
  dbops/notifications.py — NotificationsMixin (notif_v2, action_items)
  dbops/selfheal.py      — SelfhealMixin (error_events)
  dbops/agents.py        — AgentsMixin   (agent pool, tool telemetry, watchdog events)
  dbops/runs.py          — RunsMixin     (durable agent I/O ledger: agent_runs)
"""

import logging
import sqlite3
from pathlib import Path

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Re-export public names from sub-modules so callers don't need to change.
# ---------------------------------------------------------------------------
from dbops.schema import (  # noqa: E402, F401
    CREATE_ACTION_ITEMS,
    CREATE_AGENT_COMPLETIONS,
    CREATE_AGENT_TOOL_EVENTS,
    CREATE_AGENTS,
    CREATE_ERROR_EVENTS,
    CREATE_GRAPH_EDGES,
    CREATE_GRAPH_TASKS,
    CREATE_GRAPH_TOPICS,
    CREATE_MESSAGES,
    CREATE_NOTIFICATIONS,
    CREATE_NOTIFICATIONS_V2,
    CREATE_PROJECT_CORRECTIONS,
    CREATE_PROJECTS,
    CREATE_SESSION,
    CREATE_SETTINGS,
    CREATE_THREADS,
    CREATE_WATCHDOG_EVENTS,
    DB_PATH,
    DEFAULT_DATA_DIR,
    INBOX_PROJECT_ID,
    MAX_BACKGROUND_AGENTS,
    MAX_THREADS,
    _is_junk_message,
    _now,
    _thread_age_seconds,
)
from dbops.agents import AgentsMixin  # noqa: E402, F401
from dbops.messages import MessagesMixin  # noqa: E402, F401
from dbops.migrations import run_migrations  # noqa: E402, F401
from dbops.notifications import NotificationsMixin  # noqa: E402, F401
from dbops.projects import ProjectsMixin  # noqa: E402, F401
from dbops.runs import RunsMixin  # noqa: E402, F401
from dbops.selfheal import SelfhealMixin  # noqa: E402, F401
from dbops.session import SessionMixin  # noqa: E402, F401
from dbops.threads import ThreadsMixin  # noqa: E402, F401

# ---------------------------------------------------------------------------
# Composed JuggleDB class
# ---------------------------------------------------------------------------


class JuggleDB(
    SessionMixin,
    ThreadsMixin,
    ProjectsMixin,
    MessagesMixin,
    NotificationsMixin,
    SelfhealMixin,
    AgentsMixin,
    RunsMixin,
):
    """Full juggle database interface — all domain mixins assembled here.

    Entry point: ``JuggleDB(db_path=None)`` where db_path defaults to
    ``DB_PATH`` (``~/.claude/juggle/juggle.db`` by default).
    """

    def __init__(self, db_path=None):
        if db_path is None:
            # Honor JUGGLE_DB_PATH at call time so test isolation (which sets the
            # env per-test) redirects bare JuggleDB() off the production DB.
            from dbops.schema import _resolve_db_path
            self.db_path = _resolve_db_path()
        else:
            self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        # Corruption-hardening, applied on EVERY connection (the one chokepoint
        # all CLI/hooks/watchdog/test callers funnel through):
        #   journal_mode=WAL  — persisted in the file header; concurrent readers
        #     + single writer, which suits juggle's multi-agent access.
        #   synchronous=NORMAL — PER-CONNECTION (not persisted), so it MUST be
        #     re-asserted every connect. WAL-safe (no corruption; only risks last txn on power loss); relaxed from FULL 2026-06-10 — FULL fsync-per-commit caused integrate lock-hold storms under concurrent agents.
        #   busy_timeout=5000 — WAL still serializes writers and juggle opens
        #     many concurrent connections; prevents spurious "database is locked".
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def init_db(self):
        """Create tables if not exist, run schema migrations, enable WAL mode.

        G2: refuse to migrate the shared production DB from an agent/worktree
        context (only the orchestrator migrates the shared DB). Raises
        SharedDBMigrationRefused before any DDL touches the shared file.
        """
        from dbops.graph_guards import assert_migration_allowed

        assert_migration_allowed(self.db_path)
        with self._connect() as conn:
            # WAL/synchronous/busy_timeout are set by _connect() on every open.
            conn.execute(CREATE_THREADS)
            conn.execute(CREATE_MESSAGES)
            conn.execute(CREATE_NOTIFICATIONS)
            conn.execute(CREATE_SESSION)
            conn.execute(CREATE_AGENTS)
            conn.execute(CREATE_NOTIFICATIONS_V2)
            conn.execute(CREATE_ACTION_ITEMS)
            conn.execute(CREATE_SETTINGS)
            conn.execute(CREATE_AGENT_COMPLETIONS)
            conn.execute(CREATE_WATCHDOG_EVENTS)
            conn.execute(CREATE_AGENT_TOOL_EVENTS)
            conn.execute(CREATE_ERROR_EVENTS)
            conn.execute(CREATE_PROJECTS)
            conn.execute(CREATE_GRAPH_TASKS)
            conn.execute(CREATE_GRAPH_EDGES)
            conn.execute(CREATE_GRAPH_TOPICS)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_graph_tasks_project_state "
                "ON graph_tasks(project_id, state)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_graph_tasks_thread "
                "ON graph_tasks(thread_id) WHERE thread_id IS NOT NULL"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_error_events_sig "
                "ON error_events(signature_hash) WHERE status != 'resolved'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_error_events_status "
                "ON error_events(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notifications_v2_session "
                "ON notifications_v2(session_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notifications_v2_thread "
                "ON notifications_v2(thread_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_action_items_open "
                "ON action_items(dismissed_at) WHERE dismissed_at IS NULL"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_action_items_thread "
                "ON action_items(thread_id)"
            )
            run_migrations(conn)
            conn.commit()

    # _migrate kept as a shim for any callers that patched it in tests
    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Shim: delegates to dbops.migrations.run_migrations."""
        run_migrations(conn)
