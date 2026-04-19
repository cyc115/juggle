#!/usr/bin/env python3
"""Juggle DB - SQLite state manager for multi-topic conversation orchestrator."""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

from juggle_settings import get_settings as _get_settings  # noqa: E402

MAX_THREADS: int = _get_settings()["max_threads"]
MAX_BACKGROUND_AGENTS: int = _get_settings()["max_agents"]

DEFAULT_DATA_DIR = Path(_get_settings()["paths"]["data_dir"])
DB_PATH = DEFAULT_DATA_DIR / "juggle.db"

CREATE_THREADS = """
CREATE TABLE IF NOT EXISTS threads (
  id              TEXT PRIMARY KEY,
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
  title           TEXT DEFAULT '',
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

CREATE_AGENTS = """
CREATE TABLE IF NOT EXISTS agents (
  id              TEXT PRIMARY KEY,
  role            TEXT NOT NULL,
  pane_id         TEXT NOT NULL,
  assigned_thread TEXT,
  status          TEXT NOT NULL DEFAULT 'idle',
  context_threads TEXT NOT NULL DEFAULT '[]',
  created_at      TEXT NOT NULL,
  last_active     TEXT NOT NULL
);
"""

CREATE_DOMAINS = """
CREATE TABLE IF NOT EXISTS domains (
  name  TEXT PRIMARY KEY
);
"""

CREATE_DOMAIN_PATHS = """
CREATE TABLE IF NOT EXISTS domain_paths (
  path_fragment TEXT NOT NULL PRIMARY KEY,
  domain        TEXT NOT NULL REFERENCES domains(name)
);
"""

_INITIAL_DOMAINS: list[str] = _get_settings()["domains"]["initial_domains"]
_INITIAL_DOMAIN_PATHS: list[tuple[str, str]] = [
    (p, d) for p, d in _get_settings()["domains"]["initial_domain_paths"]
]

CREATE_NOTIFICATIONS_V2 = """
CREATE TABLE IF NOT EXISTS notifications_v2 (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id       TEXT,
  message         TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  session_id      TEXT NOT NULL,
  FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE SET NULL
);
"""

CREATE_ACTION_ITEMS = """
CREATE TABLE IF NOT EXISTS action_items (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id       TEXT,
  message         TEXT NOT NULL,
  type            TEXT NOT NULL,
  priority        TEXT NOT NULL DEFAULT 'normal',
  created_at      TEXT NOT NULL,
  dismissed_at    TEXT,
  FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE SET NULL
);
"""

CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
  key             TEXT PRIMARY KEY,
  value           TEXT NOT NULL
);
"""


def _next_excel_label(used: set) -> str:
    """Return first unused Excel-style base-26 label: A..Z, AA..AZ, BA..ZZ."""
    import string
    letters = string.ascii_uppercase
    # Single letter
    for c in letters:
        if c not in used:
            return c
    # Two letters AA..ZZ
    for c1 in letters:
        for c2 in letters:
            label = c1 + c2
            if label not in used:
                return label
    raise ValueError("All 702 user labels in use. Archive threads first.")


def _thread_age_seconds(last_active: str | None) -> float | None:
    """Parse last_active ISO timestamp, return seconds since now, or None."""
    if not last_active:
        return None
    try:
        dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError):
        return None


def _is_junk_message(content: str) -> bool:
    """Return True if content is a junk/system message to be excluded from display."""
    return (
        content.startswith("<task-notification")
        or "</task-notification>" in content
        or "task-id" in content
        or "<tool_uses>" in content
        or content.strip().startswith("/")
    )


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
            conn.execute(CREATE_NOTIFICATIONS)
            conn.execute(CREATE_SESSION)
            conn.execute(CREATE_AGENTS)
            conn.execute(CREATE_DOMAINS)
            conn.execute(CREATE_DOMAIN_PATHS)
            conn.execute(CREATE_NOTIFICATIONS_V2)
            conn.execute(CREATE_ACTION_ITEMS)
            conn.execute(CREATE_SETTINGS)
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
            except sqlite3.OperationalError as e:
                _log.warning("Migration 2 skipped: %s", e)

        # Migration 3 (new DBs): add show_in_list if missing
        if "show_in_list" not in cols:
            try:
                conn.execute("ALTER TABLE threads ADD COLUMN show_in_list INTEGER NOT NULL DEFAULT 1")
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
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "agents" not in tables:
            conn.execute(CREATE_AGENTS)

        # Migration 7: add domain to threads
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
        if "domain" not in cols:
            try:
                conn.execute("ALTER TABLE threads ADD COLUMN domain TEXT DEFAULT NULL")
            except sqlite3.OperationalError as e:
                _log.warning("Migration 7 skipped: %s", e)

        # Migration 8: add domain to agents
        agent_cols = {row["name"] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
        if "domain" not in agent_cols:
            try:
                conn.execute("ALTER TABLE agents ADD COLUMN domain TEXT DEFAULT NULL")
            except sqlite3.OperationalError as e:
                _log.warning("Migration 8 skipped: %s", e)

        # Migration 9: seed domains + domain_paths tables if empty
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "domains" in tables:
            existing_domains = {row[0] for row in conn.execute("SELECT name FROM domains").fetchall()}
            for name in _INITIAL_DOMAINS:
                if name not in existing_domains:
                    conn.execute("INSERT OR IGNORE INTO domains (name) VALUES (?)", (name,))
        if "domain_paths" in tables:
            existing_paths = {row[0] for row in conn.execute(
                "SELECT path_fragment FROM domain_paths"
            ).fetchall()}
            for path_fragment, domain in _INITIAL_DOMAIN_PATHS:
                if path_fragment not in existing_paths:
                    conn.execute(
                        "INSERT OR IGNORE INTO domain_paths (path_fragment, domain) VALUES (?, ?)",
                        (path_fragment, domain),
                    )

        # Migration 10: add memory columns for Hindsight integration
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
        if "memory_loaded" not in cols:
            try:
                conn.execute("ALTER TABLE threads ADD COLUMN memory_context TEXT DEFAULT ''")
                conn.execute("ALTER TABLE threads ADD COLUMN memory_loaded INTEGER DEFAULT 0")
            except sqlite3.OperationalError as e:
                _log.warning("Migration 10 skipped: %s", e)

        # Migration 11: add delivery_attempts to notifications for escalation
        notif_cols = {row["name"] for row in conn.execute("PRAGMA table_info(notifications)").fetchall()}
        if "delivery_attempts" not in notif_cols:
            try:
                conn.execute("ALTER TABLE notifications ADD COLUMN delivery_attempts INTEGER DEFAULT 0")
            except sqlite3.OperationalError as e:
                _log.warning("Migration 11 skipped: %s", e)

        # Migration 12: add reviewed flag for cockpit REVIEW nudge
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
        if "reviewed" not in cols:
            try:
                conn.execute("ALTER TABLE threads ADD COLUMN reviewed INTEGER DEFAULT 0")
            except sqlite3.OperationalError as e:
                _log.warning("Migration 12 skipped: %s", e)

        # Migration 13: add severity column for cockpit notification routing
        notif_cols = {row["name"] for row in conn.execute("PRAGMA table_info(notifications)").fetchall()}
        if "severity" not in notif_cols:
            try:
                conn.execute("ALTER TABLE notifications ADD COLUMN severity TEXT DEFAULT 'action'")
            except sqlite3.OperationalError as e:
                _log.warning("Migration 13 skipped: %s", e)

        # Migration 14: add user_label + last_active_at to threads
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
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
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
        if "label" in cols:
            try:
                # Step 1: backfill user_label for any threads still missing one
                used = {row["user_label"] for row in conn.execute(
                    "SELECT user_label FROM threads WHERE user_label IS NOT NULL"
                ).fetchall()}
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
                # Step 2: drop the now-redundant label column
                conn.execute("ALTER TABLE threads DROP COLUMN label")
                _log.info("Migration 16: backfilled %d threads, dropped label column", len(missing))
            except sqlite3.OperationalError as e:
                _log.warning("Migration 16 skipped: %s", e)

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

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        """Return a value from the settings table, or default if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def _set_session_key_external(self, key: str, value: str):
        """Public helper to write a session key (for tests)."""
        with self._connect() as conn:
            self._set_session_key(conn, key, value)
            conn.commit()

    # ------------------------------------------------------------------
    # Thread operations
    # ------------------------------------------------------------------

    def create_thread(self, topic: str, session_id: str, domain: str | None = None) -> str:
        """Create a new thread. Returns the UUID of the new thread.

        Assigns next available A–Z label. Raises ValueError if 10 non-archived
        threads already exist or all 26 labels are in use.
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT id, status FROM threads").fetchall()
            active_count = sum(1 for row in rows if row["status"] != "archived")
            if active_count >= MAX_THREADS:
                candidates = self.get_archive_candidates()
                if candidates:
                    cmds = ", ".join(
                        f"[{t.get('user_label') or t.get('label')}] "
                        f"{(t.get('title') or t.get('topic') or '')[:40]}"
                        f" → archive-thread {t.get('user_label') or t.get('label')}"
                        for t in candidates[:5]
                    )
                    raise ValueError(
                        f"Maximum of {MAX_THREADS} threads already exist. "
                        f"Archivable: {cmds}"
                    )
                else:
                    raise ValueError(
                        f"Maximum of {MAX_THREADS} threads already exist. "
                        "No immediate candidates — close or archive a thread manually."
                    )
            new_id = str(uuid.uuid4())
            used_labels = {row["user_label"] for row in conn.execute(
                "SELECT user_label FROM threads WHERE user_label IS NOT NULL"
            ).fetchall()}
            user_label = _next_excel_label(used_labels)
            now_iso = datetime.now(timezone.utc).isoformat()
            now_min = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            conn.execute(
                """
                INSERT INTO threads
                  (id, user_label, session_id, topic, status,
                   summary, key_decisions, open_questions,
                   last_user_intent, agent_task_id, agent_result,
                   show_in_list, summarized_msg_count, domain, created_at, last_active, last_active_at)
                VALUES (?, ?, ?, ?, 'active', '', '[]', '[]', '', NULL, NULL, 1, 0, ?, ?, ?, ?)
                """,
                (new_id, user_label, session_id, topic, domain, now_iso, now_iso, now_min),
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

    def get_thread_by_user_label(self, label: str) -> dict | None:
        """Look up a thread by its user_label (e.g. 'A', 'BC'). Case-insensitive. Prefers UUID ids over legacy non-UUID ids."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM threads WHERE user_label = ? ORDER BY "
                "CASE WHEN length(id) = 36 AND id LIKE '%-%-%-%-%' THEN 0 ELSE 1 END, "
                "last_active_at DESC LIMIT 1",
                (label.upper(),)
            ).fetchone()
        return dict(row) if row else None


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
    # Thread state machine
    # ------------------------------------------------------------------

    _VALID_STATES = {"active", "running", "closed", "archived"}

    def set_thread_status(self, thread_id: str, status: str) -> None:
        """Transition a thread to a new state ({'active','running','closed','archived'}).

        Updates last_active_at to now (UTC, minute precision).
        Raises ValueError for any other status value.
        """
        if status not in self._VALID_STATES:
            raise ValueError(
                f"invalid status {status!r}; must be one of {sorted(self._VALID_STATES)}"
            )
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            conn.execute(
                "UPDATE threads SET status = ?, last_active_at = ? WHERE id = ?",
                (status, now, thread_id),
            )
            conn.commit()

    def touch_last_active(self, thread_id: str) -> None:
        """Update last_active_at to now without changing status."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            conn.execute(
                "UPDATE threads SET last_active_at = ? WHERE id = ?",
                (now, thread_id),
            )
            conn.commit()

    def get_threads_by_status(self, status: str) -> list[dict]:
        """Return all threads matching the given status. Order: last_active_at DESC."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM threads WHERE status = ? ORDER BY last_active_at DESC",
                (status,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Message operations
    # ------------------------------------------------------------------

    def get_messages(
        self, thread_id: str, token_budget: int | None = None
    ) -> list[dict]:
        """
        Load messages newest-first until token budget is exhausted
        (token estimate = len(content) // 4), then return in chronological order.
        """
        budget: int = token_budget if token_budget is not None else int(_get_settings()["message_history_token_budget"])
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
        remaining: int = budget
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
            if not _is_junk_message(row["content"]):
                count += 1
        return count

    # ------------------------------------------------------------------
    # Shared context
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

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
            if not _is_junk_message(row["content"]):
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
            if row["role"] == "user" and not _is_junk_message(row["content"])
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

    def get_stale_threads(self, threshold: int | None = None) -> list[dict]:
        """Return threads where substantive user message delta >= threshold.

        Uses a single DB query for all threads instead of N per-thread calls.
        """
        limit: int = threshold if threshold is not None else int(_get_settings()["stale_summary_message_threshold"])
        threads = self.get_all_threads()
        if not threads:
            return []

        thread_ids = [t["id"] for t in threads]
        placeholders = ", ".join("?" * len(thread_ids))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT thread_id, content FROM messages "
                f"WHERE thread_id IN ({placeholders}) AND role = 'user'",
                thread_ids,
            ).fetchall()

        # Count non-junk messages per thread in Python
        counts: dict[str, int] = {}
        for row in rows:
            if not _is_junk_message(row["content"]):
                counts[row["thread_id"]] = counts.get(row["thread_id"], 0) + 1

        stale = []
        for t in threads:
            tid = t["id"]
            msg_count = counts.get(tid, 0)
            summarized: int = int(t.get("summarized_msg_count") or 0)
            delta: int = msg_count - summarized
            if delta >= limit:
                stale.append({**t, "delta": delta, "msg_count": msg_count})
        return stale

    # ------------------------------------------------------------------
    # Notifications v2 (session-scoped, spec schema)
    # ------------------------------------------------------------------

    def add_notification_v2(self, thread_id, message: str, session_id: str) -> int:
        """Insert a notifications_v2 row. Returns new id."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO notifications_v2 (thread_id, message, created_at, session_id) "
                "VALUES (?, ?, ?, ?)",
                (thread_id, message, now, session_id),
            )
            conn.commit()
            return cur.lastrowid

    def get_notifications_for_session(self, session_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, thread_id, message, created_at, session_id "
                "FROM notifications_v2 WHERE session_id = ? ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_notifications_v2_for_other_sessions(self, current_session_id: str) -> int:
        """Delete notifications_v2 whose session_id != current. Returns rows deleted."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM notifications_v2 WHERE session_id != ?",
                (current_session_id,),
            )
            conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # Action items
    # ------------------------------------------------------------------

    def add_action_item(self, thread_id, message: str,
                        type_: str, priority: str = "normal") -> int:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO action_items (thread_id, message, type, priority, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (thread_id, message, type_, priority, now),
            )
            conn.commit()
            return cur.lastrowid

    def get_open_action_items(self) -> list[dict]:
        """Open action items ordered by (priority: high > normal > low), then created_at DESC."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, thread_id, message, type, priority, created_at, dismissed_at
                FROM action_items
                WHERE dismissed_at IS NULL
                ORDER BY
                  CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                  created_at DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def dismiss_action_item(self, action_id: int) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            conn.execute(
                "UPDATE action_items SET dismissed_at = ? WHERE id = ?",
                (now, action_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Archive operations
    # ------------------------------------------------------------------

    def archive_thread(self, thread_id: str):
        """Set status='archived', show_in_list=0. Preserves user_label."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            conn.execute(
                "UPDATE threads SET status = 'archived', "
                "show_in_list = 0, last_active_at = ? WHERE id = ?",
                (now, thread_id),
            )
            conn.commit()

    def unarchive_thread(self, thread_id: str) -> str:
        """Unarchive: status=active, show_in_list=1, user_label preserved."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            conn.execute(
                "UPDATE threads SET status = 'active', show_in_list = 1, "
                "last_active_at = ? WHERE id = ?",
                (now, thread_id),
            )
            conn.commit()
        thread = self.get_thread(thread_id)
        return thread.get("user_label") or thread_id[:8] if thread else thread_id[:8]

    # ------------------------------------------------------------------
    # Agent pool operations
    # ------------------------------------------------------------------

    def create_agent(self, role: str, pane_id: str) -> str:
        """Create a new agent record. Returns the agent UUID."""
        new_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agents
                  (id, role, pane_id, assigned_thread, status, context_threads, created_at, last_active)
                VALUES (?, ?, ?, NULL, 'idle', '[]', ?, ?)
                """,
                (new_id, role, pane_id, now, now),
            )
            conn.commit()
        return new_id

    def get_agent(self, agent_id: str) -> dict | None:
        """Look up an agent by UUID. Returns None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_all_agents(self) -> list[dict]:
        """Return all agents ordered by creation time."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agents ORDER BY created_at"
            ).fetchall()
            return [dict(row) for row in rows]

    def update_agent(self, agent_id: str, **kwargs):
        """Update any column(s) on an agent row. Serializes list values to JSON."""
        if not kwargs:
            return
        serialized = {
            k: json.dumps(v) if isinstance(v, list) else v
            for k, v in kwargs.items()
        }
        set_clause = ", ".join(f"{col} = ?" for col in serialized)
        values = list(serialized.values()) + [agent_id]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE agents SET {set_clause} WHERE id = ?",
                values,
            )
            conn.commit()

    def delete_agent(self, agent_id: str):
        """Delete an agent record."""
        with self._connect() as conn:
            conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            conn.commit()

    def get_best_agent(self, thread_id: str, role: str | None = None,
                       domain: str | None = None) -> dict | None:
        """Return the best idle agent for a given thread using scoring.

        Domain filtering (applied before scoring):
          - domain is non-null → only agents with matching domain OR null domain
          - domain is null → only agents with null domain (fresh/unassigned)

        Scoring (higher = better):
          +2 if thread_id is in agent's context_threads (has existing context)
          +1 if agent's role matches the requested role

        Ties broken by most recent last_active.
        Returns None if no suitable idle agents exist.
        """
        idle = [a for a in self.get_all_agents() if a["status"] == "idle"]
        if not idle:
            return None

        if domain:
            # Non-null domain: accept agents with matching domain or null domain
            idle = [
                a for a in idle
                if a.get("domain") is None or a.get("domain") == domain
            ]
        else:
            # Null domain thread: only fresh agents (domain=null) to avoid cross-pollination
            idle = [a for a in idle if a.get("domain") is None]

        if not idle:
            logging.info("domain filter: no idle '%s' agents, will spawn fresh", domain)
            return None

        def _score(agent: dict) -> tuple:
            context = json.loads(agent.get("context_threads") or "[]")
            s = 0
            if thread_id in context:
                s += 2
            if role and agent["role"] == role:
                s += 1
            return (s, agent["last_active"])

        return max(idle, key=_score)

    # ------------------------------------------------------------------
    # Domain registry
    # ------------------------------------------------------------------

    def register_domain(self, name: str) -> None:
        """Insert domain name into domains table. No-op if already exists."""
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO domains (name) VALUES (?)", (name,))
            conn.commit()

    def get_domains(self) -> list[str]:
        """Return all registered domain names."""
        with self._connect() as conn:
            rows = conn.execute("SELECT name FROM domains ORDER BY name").fetchall()
            return [row["name"] for row in rows]

    def is_known_domain(self, name: str) -> bool:
        """Return True if name is a registered domain."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name FROM domains WHERE name = ?", (name,)
            ).fetchone()
            return row is not None

    def add_domain_path(self, path_fragment: str, domain: str) -> None:
        """Insert or replace a path_fragment → domain mapping."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO domain_paths (path_fragment, domain) VALUES (?, ?)",
                (path_fragment, domain),
            )
            conn.commit()

    def get_domain_paths(self) -> list[dict]:
        """Return all path→domain mappings."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT path_fragment, domain FROM domain_paths ORDER BY path_fragment"
            ).fetchall()
            return [dict(row) for row in rows]

    def infer_domain_from_prompt(self, prompt: str) -> str | None:
        """Return first domain whose path_fragment appears in prompt, or None."""
        mappings = self.get_domain_paths()
        for m in mappings:
            if m["path_fragment"] in prompt:
                return m["domain"]
        return None

    def get_agent_by_thread(self, thread_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE assigned_thread = ? AND status = 'busy' LIMIT 1",
                (thread_id,)
            ).fetchone()
            return dict(row) if row else None

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
        candidates = []
        for t in threads:
            tid = t["id"]
            status = t.get("status") or "active"

            if tid == current_thread or status == "archived":
                continue

            if status in ("done", "failed", "closed"):
                candidates.append(t)
                continue

            age = _thread_age_seconds(t.get("last_active") or "")
            if age is not None and age > _get_settings()["thread_archive_threshold_secs"] and status not in ("background", "waiting"):
                candidates.append(t)

        return candidates
