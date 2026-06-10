#!/usr/bin/env python3
"""Juggle DB — SQLite state manager for the multi-topic conversation orchestrator.

Composition root: assembles JuggleDB from focused mixin modules and re-exports
all public names so every existing ``from juggle_db import X`` continues to
work without caller changes.

Domain modules (each ≤300 lines):
  juggle_db_schema.py        — DDL, module constants, pure helpers
  juggle_db_migrations.py    — incremental schema migration runner (run_migrations)
  juggle_db_session.py       — SessionMixin  (session KV, active flag, settings)
  juggle_db_threads.py       — ThreadsMixin  (thread CRUD, state machine, archive)
  juggle_db_projects.py      — ProjectsMixin (project CRUD, match-profile, corrections)
  juggle_db_messages.py      — MessagesMixin (message storage, context-window queries)
  juggle_db_notifications.py — NotificationsMixin (notif_v2, action_items)
  juggle_db_selfheal.py      — SelfhealMixin (error_events)
  juggle_db_agents.py        — AgentsMixin   (agent pool, tool telemetry, watchdog events)
"""

import logging
import sqlite3
from pathlib import Path

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Re-export public names from sub-modules so callers don't need to change.
# ---------------------------------------------------------------------------
from juggle_db_schema import (  # noqa: E402, F401
    CREATE_ACTION_ITEMS,
    CREATE_AGENT_COMPLETIONS,
    CREATE_AGENT_TOOL_EVENTS,
    CREATE_AGENTS,
    CREATE_ERROR_EVENTS,
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
    MAX_ACTION_NOTIF_LENGTH,
    MAX_BACKGROUND_AGENTS,
    MAX_THREADS,
    _POINTER_SUFFIX,
    _is_junk_message,
    _next_excel_label,
    _now,
    _thread_age_seconds,
)
from juggle_db_agents import AgentsMixin  # noqa: E402, F401
from juggle_db_messages import MessagesMixin  # noqa: E402, F401
from juggle_db_migrations import run_migrations  # noqa: E402, F401
from juggle_db_notifications import NotificationsMixin  # noqa: E402, F401
from juggle_db_projects import ProjectsMixin  # noqa: E402, F401
from juggle_db_selfheal import SelfhealMixin  # noqa: E402, F401
from juggle_db_session import SessionMixin  # noqa: E402, F401
from juggle_db_threads import ThreadsMixin  # noqa: E402, F401

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
):
    """Full juggle database interface — all domain mixins assembled here.

    Entry point: ``JuggleDB(db_path=None)`` where db_path defaults to
    ``DB_PATH`` (``~/.claude/juggle/juggle.db`` by default).
    """

    def __init__(self, db_path=None):
        if db_path is None:
            self.db_path = DB_PATH
        else:
            self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        """Create tables if not exist, run schema migrations, enable WAL mode."""
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
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
        """Shim: delegates to juggle_db_migrations.run_migrations."""
        run_migrations(conn)
