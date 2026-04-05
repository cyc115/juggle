#!/usr/bin/env python3
"""Juggle DB - SQLite state manager for multi-topic conversation orchestrator."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path.home() / ".claude" / "juggle" / "juggle.db"

THREAD_IDS = ["A", "B", "C", "D"]

CREATE_THREADS = """
CREATE TABLE IF NOT EXISTS threads (
  thread_id       TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL,
  topic           TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'active',
  summary         TEXT DEFAULT '',
  key_decisions   TEXT DEFAULT '[]',
  open_questions  TEXT DEFAULT '[]',
  last_user_intent TEXT DEFAULT '',
  agent_task_id   TEXT,
  agent_result    TEXT,
  created_at      TEXT NOT NULL,
  last_active     TEXT NOT NULL
);
"""

CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id       TEXT NOT NULL REFERENCES threads(thread_id),
  role            TEXT NOT NULL,
  content         TEXT NOT NULL,
  token_estimate  INTEGER DEFAULT 0,
  created_at      TEXT NOT NULL
);
"""

CREATE_SHARED_CONTEXT = """
CREATE TABLE IF NOT EXISTS shared_context (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  context_type    TEXT NOT NULL,
  content         TEXT NOT NULL,
  source_thread   TEXT,
  created_at      TEXT NOT NULL
);
"""

CREATE_NOTIFICATIONS = """
CREATE TABLE IF NOT EXISTS notifications (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id       TEXT NOT NULL REFERENCES threads(thread_id),
  message         TEXT NOT NULL,
  delivered       INTEGER DEFAULT 0,
  created_at      TEXT NOT NULL
);
"""

CREATE_SESSION = """
CREATE TABLE IF NOT EXISTS session (
  key             TEXT PRIMARY KEY,
  value           TEXT NOT NULL
);
"""


class JuggleDB:
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
        """Create tables if not exist, enable WAL mode."""
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(CREATE_THREADS)
            conn.execute(CREATE_MESSAGES)
            conn.execute(CREATE_SHARED_CONTEXT)
            conn.execute(CREATE_NOTIFICATIONS)
            conn.execute(CREATE_SESSION)
            conn.commit()

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def _get_session_key(self, conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute(
            "SELECT value FROM session WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def _set_session_key(self, conn: sqlite3.Connection, key: str, value: str):
        conn.execute(
            "INSERT INTO session(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def is_active(self) -> bool:
        """Return True if session.active == '1'."""
        with self._connect() as conn:
            return self._get_session_key(conn, "active") == "1"

    def set_active(self, val: bool):
        with self._connect() as conn:
            self._set_session_key(conn, "active", "1" if val else "0")
            if val and self._get_session_key(conn, "started_at") is None:
                self._set_session_key(
                    conn, "started_at", datetime.now(timezone.utc).isoformat()
                )
            conn.commit()

    def get_current_thread(self) -> str | None:
        with self._connect() as conn:
            return self._get_session_key(conn, "current_thread")

    def set_current_thread(self, thread_id: str):
        with self._connect() as conn:
            self._set_session_key(conn, "current_thread", thread_id)
            conn.commit()

    # ------------------------------------------------------------------
    # Thread operations
    # ------------------------------------------------------------------

    def create_thread(self, topic: str, session_id: str) -> str:
        """Auto-assign next letter A/B/C/D. Raises ValueError if 4 threads exist."""
        with self._connect() as conn:
            existing = {
                row["thread_id"]
                for row in conn.execute("SELECT thread_id FROM threads").fetchall()
            }
            if len(existing) >= 4:
                raise ValueError(
                    "Maximum of 4 threads already exist. "
                    "Close or complete a thread before creating a new one."
                )
            for letter in THREAD_IDS:
                if letter not in existing:
                    now = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        """
                        INSERT INTO threads
                          (thread_id, session_id, topic, status,
                           summary, key_decisions, open_questions,
                           last_user_intent, agent_task_id, agent_result,
                           created_at, last_active)
                        VALUES (?, ?, ?, 'active', '', '[]', '[]', '', NULL, NULL, ?, ?)
                        """,
                        (letter, session_id, topic, now, now),
                    )
                    conn.commit()
                    return letter
        # Should never reach here given the guard above
        raise ValueError("No available thread slot found.")

    def get_thread(self, thread_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if row is None:
                return None
            return dict(row)

    def get_all_threads(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM threads ORDER BY thread_id"
            ).fetchall()
            return [dict(row) for row in rows]

    def update_thread(self, thread_id: str, **kwargs):
        """Update any column(s) on a thread row."""
        if not kwargs:
            return
        # Serialize list values to JSON
        for key, val in kwargs.items():
            if isinstance(val, list):
                kwargs[key] = json.dumps(val)
        set_clause = ", ".join(f"{col} = ?" for col in kwargs)
        values = list(kwargs.values()) + [thread_id]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE threads SET {set_clause} WHERE thread_id = ?",
                values,
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Message operations
    # ------------------------------------------------------------------

    def get_messages(
        self, thread_id: str, token_budget: int = 1500
    ) -> list[dict]:
        """
        Load messages newest-first until token budget is exhausted
        (token estimate = len(content) // 4), then return in chronological order.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, thread_id, role, content, token_estimate, created_at
                FROM messages
                WHERE thread_id = ?
                ORDER BY id DESC
                """,
                (thread_id,),
            ).fetchall()

        selected = []
        remaining = token_budget
        for row in rows:
            estimate = len(row["content"]) // 4
            if estimate > remaining:
                break
            selected.append(dict(row))
            remaining -= estimate

        # Return in chronological order
        selected.reverse()
        return selected

    def add_message(self, thread_id: str, role: str, content: str):
        token_estimate = len(content) // 4
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (thread_id, role, content, token_estimate, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (thread_id, role, content, token_estimate, now),
            )
            # Also update last_active on the thread
            conn.execute(
                "UPDATE threads SET last_active = ? WHERE thread_id = ?",
                (now, thread_id),
            )
            conn.commit()

    def get_message_count(self, thread_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM messages WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            return row["cnt"] if row else 0

    # ------------------------------------------------------------------
    # Shared context
    # ------------------------------------------------------------------

    def add_shared(
        self,
        context_type: str,
        content: str,
        source_thread: str | None = None,
    ):
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO shared_context (context_type, content, source_thread, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (context_type, content, source_thread, now),
            )
            conn.commit()

    def get_shared_context(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM shared_context ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def add_notification(self, thread_id: str, message: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notifications (thread_id, message, delivered, created_at)
                VALUES (?, ?, 0, ?)
                """,
                (thread_id, message, now),
            )
            conn.commit()

    def get_pending_notifications(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notifications WHERE delivered = 0 ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_notifications_delivered(self, ids: list[int]):
        if not ids:
            return
        placeholders = ", ".join("?" * len(ids))
        with self._connect() as conn:
            conn.execute(
                f"UPDATE notifications SET delivered = 1 WHERE id IN ({placeholders})",
                ids,
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Background agents
    # ------------------------------------------------------------------

    def get_background_agents(self) -> list[dict]:
        """Return threads with status='background' and agent_task_id set."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM threads
                WHERE status = 'background'
                  AND agent_task_id IS NOT NULL
                ORDER BY thread_id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_last_exchange(self, thread_id: str) -> dict:
        """Return the last user message and last assistant message for a thread.

        Returns a dict with keys:
            last_user, last_user_at, last_assistant, last_assistant_at
        Values are None when no message of that role exists.
        """
        with self._connect() as conn:
            user_row = conn.execute(
                """
                SELECT content, created_at FROM messages
                WHERE thread_id = ? AND role = 'user'
                ORDER BY id DESC LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
            assistant_row = conn.execute(
                """
                SELECT content, created_at FROM messages
                WHERE thread_id = ? AND role = 'assistant'
                ORDER BY id DESC LIMIT 1
                """,
                (thread_id,),
            ).fetchone()

        result = {
            "last_user": user_row["content"] if user_row else None,
            "last_user_at": user_row["created_at"] if user_row else None,
            "last_assistant": assistant_row["content"] if assistant_row else None,
            "last_assistant_at": assistant_row["created_at"] if assistant_row else None,
        }

        # Fallback: if no assistant message is stored yet, use agent_result from the thread.
        if not result["last_assistant"]:
            thread = self.get_thread(thread_id)
            if thread and thread.get("agent_result"):
                result["last_assistant"] = thread["agent_result"]
                result["last_assistant_at"] = thread.get("last_active")

        return result

    def update_thread_summary(self, thread_id: str, summary: str):
        """Write a summary string to threads.summary."""
        self.update_thread(thread_id, summary=summary)
