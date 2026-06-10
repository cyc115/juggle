"""juggle_db_session — Session-key helpers mixin for JuggleDB.

Owns: reading/writing the key-value `session` table, active flag, current
thread, orchestrator session ID + heartbeat, and the settings table reader.
Must not own: thread/agent/message operations (see other mixin modules).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


class SessionMixin:
    """Mixin for session-scoped state (active flag, current thread, settings)."""

    # ---------------------------------------------------------------
    # Low-level session table helpers (internal)
    # ---------------------------------------------------------------

    def _get_session_key(self, conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM session WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_session_key(self, conn: sqlite3.Connection, key: str, value: str):
        conn.execute(
            "INSERT INTO session(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---------------------------------------------------------------
    # Active / current thread
    # ---------------------------------------------------------------

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

    # ---------------------------------------------------------------
    # Settings table (key/value persistence)
    # ---------------------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        """Return a value from the settings table, or default if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    # ---------------------------------------------------------------
    # Orchestrator session ID + heartbeat
    # ---------------------------------------------------------------

    def get_orchestrator_session_id(self) -> str:
        """Return the session_id of the active orchestrator, or '' if not set."""
        with self._connect() as conn:
            return self._get_session_key(conn, "orchestrator_session_id") or ""

    def set_orchestrator_session_id(self, sid: str) -> None:
        """Record (or clear) the orchestrator session ID and timestamp."""
        import time as _time

        with self._connect() as conn:
            self._set_session_key(conn, "orchestrator_session_id", sid)
            if sid:
                self._set_session_key(
                    conn, "orchestrator_session_ts", str(_time.time())
                )
            conn.commit()

    def get_orchestrator_session_ts(self) -> float:
        """Return the timestamp when the orchestrator session was last registered."""
        with self._connect() as conn:
            val = self._get_session_key(conn, "orchestrator_session_ts")
        try:
            return float(val) if val else 0.0
        except (TypeError, ValueError):
            return 0.0

    def touch_orchestrator_session_ts(self) -> None:
        """Refresh the orchestrator session heartbeat timestamp to now."""
        import time as _time

        with self._connect() as conn:
            self._set_session_key(conn, "orchestrator_session_ts", str(_time.time()))
            conn.commit()

    def _set_session_key_external(self, key: str, value: str):
        """Public helper to write a session key (for tests)."""
        with self._connect() as conn:
            self._set_session_key(conn, key, value)
            conn.commit()
