#!/usr/bin/env python3
"""Juggle DB - SQLite state manager for multi-topic conversation orchestrator."""

import json
import os as _os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_THREADS: int = int(_os.environ.get("JUGGLE_MAX_THREADS", 10))
MAX_BACKGROUND_AGENTS: int = int(_os.environ.get("JUGGLE_MAX_BACKGROUND_AGENTS", 20))

DB_PATH = Path.home() / ".claude" / "juggle" / "juggle.db"

CREATE_THREADS = """
CREATE TABLE IF NOT EXISTS threads (
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
);
"""

CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id       TEXT NOT NULL REFERENCES threads(id),
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
  thread_id       TEXT NOT NULL REFERENCES threads(id),
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


def _assign_label(conn: sqlite3.Connection) -> str:
    """Return first letter A–Z not currently held by any non-archived thread."""
    used = {row["label"] for row in conn.execute(
        "SELECT label FROM threads WHERE label IS NOT NULL"
    ).fetchall()}
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if letter not in used:
            return letter
    raise ValueError("All 26 labels in use. Archive a thread first.")


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
        """Create tables if not exist, run schema migrations, enable WAL mode."""
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(CREATE_THREADS)
            conn.execute(CREATE_MESSAGES)
            conn.execute(CREATE_SHARED_CONTEXT)
            conn.execute(CREATE_NOTIFICATIONS)
            conn.execute(CREATE_SESSION)
            self._migrate(conn)
            conn.commit()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Apply incremental schema migrations."""
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}

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
                conn.execute("ALTER TABLE threads ADD COLUMN summarized_msg_count INTEGER DEFAULT 0")
            except Exception:
                pass

        # Migration 3 (new DBs): add show_in_list if missing
        if "show_in_list" not in cols:
            try:
                conn.execute("ALTER TABLE threads ADD COLUMN show_in_list INTEGER NOT NULL DEFAULT 1")
            except Exception:
                pass

        # Migration 4 (new DBs): add label if missing
        if "label" not in cols and "id" in cols:
            try:
                conn.execute("ALTER TABLE threads ADD COLUMN label TEXT")
            except Exception:
                pass

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
        """Create a new thread. Returns the UUID of the new thread.

        Assigns next available A–Z label. Raises ValueError if 10 non-archived
        threads already exist or all 26 labels are in use.
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT id, status FROM threads").fetchall()
            active_count = sum(1 for row in rows if row["status"] != "archived")
            if active_count >= MAX_THREADS:
                raise ValueError(
                    f"Maximum of {MAX_THREADS} threads already exist. "
                    "Archive or complete a thread before creating a new one."
                )
            new_id = str(uuid.uuid4())
            label = _assign_label(conn)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO threads
                  (id, label, session_id, topic, status,
                   summary, key_decisions, open_questions,
                   last_user_intent, agent_task_id, agent_result,
                   show_in_list, summarized_msg_count, created_at, last_active)
                VALUES (?, ?, ?, ?, 'active', '', '[]', '[]', '', NULL, NULL, 1, 0, ?, ?)
                """,
                (new_id, label, session_id, topic, now, now),
            )
            conn.commit()
            return new_id

    def get_thread(self, thread_id: str) -> dict | None:
        """Look up a thread by its UUID `id`. Returns None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if row is None:
                return None
            return dict(row)

    def get_thread_by_label(self, label: str) -> dict | None:
        """Look up a thread by its display label (e.g. 'A'). Returns None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM threads WHERE label = ?", (label.upper(),)
            ).fetchone()
            if row is None:
                return None
            return dict(row)

    def get_all_threads(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM threads ORDER BY created_at"
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
                f"UPDATE threads SET {set_clause} WHERE id = ?",
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
                "UPDATE threads SET last_active = ? WHERE id = ?",
                (now, thread_id),
            )
            conn.commit()

    @staticmethod
    def _is_junk_message(content: str) -> bool:
        """Return True if content is a junk/system message to be excluded from display."""
        return (
            content.startswith("<task-notification")
            or "</task-notification>" in content
            or "task-id" in content
            or "<tool_uses>" in content
            or content.strip().startswith("/")
        )

    def get_message_count(self, thread_id: str, exclude_junk: bool = True) -> int:
        """Count user messages for a thread, optionally excluding junk."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content FROM messages WHERE thread_id = ? AND role = 'user'",
                (thread_id,),
            ).fetchall()
        if not exclude_junk:
            return len(rows)
        count = 0
        for row in rows:
            if not self._is_junk_message(row["content"]):
                count += 1
        return count

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
                ORDER BY created_at
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_last_exchange(self, thread_id: str) -> dict:
        """Return the last user message and last assistant message for a thread.

        Returns a dict with keys:
            last_user, last_user_at, last_assistant, last_assistant_at
        Values are None when no message of that role exists.
        Junk user messages (task-notifications, slash commands, etc.) are skipped.
        """
        with self._connect() as conn:
            user_rows = conn.execute(
                """
                SELECT content, created_at FROM messages
                WHERE thread_id = ? AND role = 'user'
                ORDER BY id DESC
                """,
                (thread_id,),
            ).fetchall()
            assistant_row = conn.execute(
                """
                SELECT content, created_at FROM messages
                WHERE thread_id = ? AND role = 'assistant'
                ORDER BY id DESC LIMIT 1
                """,
                (thread_id,),
            ).fetchone()

        user_row = None
        for row in user_rows:
            if not self._is_junk_message(row["content"]):
                user_row = row
                break

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

    def get_recent_exchanges(self, thread_id: str, n: int = 2) -> list[dict]:
        """Return the last n Q/A pairs for a thread, most recent first.

        Each item: {"user": str, "assistant": str | None}
        Junk user messages are skipped.
        """
        with self._connect() as conn:
            all_rows = conn.execute(
                """
                SELECT id, role, content FROM messages
                WHERE thread_id = ?
                ORDER BY id ASC
                """,
                (thread_id,),
            ).fetchall()

        # Collect non-junk user message ids in order (ascending)
        user_msgs = [
            row for row in all_rows
            if row["role"] == "user" and not self._is_junk_message(row["content"])
        ]

        # Take last n user messages (most recent first after reversing)
        recent_user_msgs = list(reversed(user_msgs[-n:])) if user_msgs else []

        result = []
        all_ids = [row["id"] for row in all_rows]
        all_by_id = {row["id"]: row for row in all_rows}

        for user_row in recent_user_msgs:
            # Find the next assistant message after this user message by id
            assistant_content: str | None = None
            for row_id in all_ids:
                if row_id > user_row["id"] and all_by_id[row_id]["role"] == "assistant":
                    assistant_content = all_by_id[row_id]["content"]
                    break
            result.append({"user": user_row["content"], "assistant": assistant_content})

        return result

    def get_thread_state(self, thread: dict, current_thread_id: str) -> str:
        """Return emoji state string for a thread dict.

        Returns one of: "👉", "🏃\u200d♂️", "⏸️", "💤", "✅", "❌", "🗄️", or "".
        Priority (highest wins): current > background > done > failed > archived > waiting > idle
        """
        tid = thread["id"]
        status = thread.get("status") or "active"
        last_active = thread.get("last_active") or ""

        # Current
        if tid == current_thread_id:
            return "👉"

        # Background (agent running)
        if status == "background":
            return "🏃\u200d♂️"

        # Done
        if status == "done":
            # Check if last assistant message is an unanswered question
            with self._connect() as conn:
                asst_row = conn.execute(
                    "SELECT id, content FROM messages WHERE thread_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
                    (tid,),
                ).fetchone()
                if asst_row and asst_row["content"].rstrip().endswith("?"):
                    user_rows = conn.execute(
                        "SELECT content FROM messages WHERE thread_id = ? AND role = 'user' AND id > ? ORDER BY id ASC",
                        (tid, asst_row["id"]),
                    ).fetchall()
                    has_real_reply = any(not self._is_junk_message(r["content"]) for r in user_rows)
                    if not has_real_reply:
                        return "⏸️"
            return "✅"

        # Failed
        if status == "failed":
            return "❌"

        # Archived: last_active > 48 hours ago
        now = datetime.now(timezone.utc)
        archived = False
        if last_active:
            try:
                dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_seconds = (now - dt).total_seconds()
                if age_seconds > 48 * 3600:
                    archived = True
            except (ValueError, TypeError):
                pass

        if archived:
            return "🗄️"

        # For waiting / idle detection we need the last assistant message
        with self._connect() as conn:
            assistant_row = conn.execute(
                """
                SELECT role, content, created_at FROM messages
                WHERE thread_id = ? AND role = 'assistant'
                ORDER BY id DESC LIMIT 1
                """,
                (tid,),
            ).fetchone()

        # Waiting: last message role == assistant AND content ends with "?"
        if assistant_row:
            if assistant_row["content"].rstrip().endswith("?"):
                return "⏸️"

        # Idle: last assistant message exists (no "?") AND last_active > 30 min ago
        if assistant_row and last_active:
            try:
                dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_seconds = (now - dt).total_seconds()
                if age_seconds > 30 * 60:
                    return "💤"
            except (ValueError, TypeError):
                pass

        return ""

    def update_thread_summary(self, thread_id: str, summary: str):
        """Write a summary string to threads.summary."""
        self.update_thread(thread_id, summary=summary)

    def set_summarized_count(self, thread_id: str, count: int) -> None:
        """Record how many messages were present at last summarization."""
        self.update_thread(thread_id, summarized_msg_count=count)

    def get_stale_threads(self, threshold: int = 3) -> list[dict]:
        """Return threads where substantive user message delta >= threshold."""
        threads = self.get_all_threads()
        stale = []
        for t in threads:
            tid = t["id"]
            msg_count = self.get_message_count(tid, exclude_junk=True)
            summarized = t.get("summarized_msg_count") or 0
            delta = msg_count - summarized
            if delta >= threshold:
                stale.append({**t, "delta": delta, "msg_count": msg_count})
        return stale

    # ------------------------------------------------------------------
    # Archive operations
    # ------------------------------------------------------------------

    def archive_thread(self, thread_id: str):
        """Set status='archived', label=NULL, show_in_list=0 for the given thread."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE threads SET status = 'archived', label = NULL, show_in_list = 0 WHERE id = ?",
                (thread_id,),
            )
            conn.commit()

    def get_archive_candidates(self) -> list[dict]:
        """Return threads that are candidates for archiving.

        A thread qualifies if ANY of:
          - status == 'done'
          - status == 'failed'
          - last_active > 48 hours ago AND status NOT IN ('background', 'waiting')
          - status == 'idle' AND last_active > 24 hours ago

        Excludes the current thread and already-archived threads.
        """
        current_thread = self.get_current_thread()
        threads = self.get_all_threads()
        now = datetime.now(timezone.utc)
        candidates = []
        for t in threads:
            tid = t["id"]
            status = t.get("status") or "active"

            # Exclude current thread
            if tid == current_thread:
                continue

            # Exclude already-archived threads
            if status == "archived":
                continue

            # Check candidate criteria
            is_candidate = False

            if status == "done":
                is_candidate = True
            elif status == "failed":
                is_candidate = True
            else:
                last_active = t.get("last_active") or ""
                if last_active:
                    try:
                        dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        age_seconds = (now - dt).total_seconds()

                        # last_active > 48 hours AND status not background/waiting
                        if age_seconds > 48 * 3600 and status not in ("background", "waiting"):
                            is_candidate = True
                        # status == 'idle' AND last_active > 24 hours
                        elif status == "idle" and age_seconds > 24 * 3600:
                            is_candidate = True
                    except (ValueError, TypeError):
                        pass

            if is_candidate:
                candidates.append(t)

        return candidates
