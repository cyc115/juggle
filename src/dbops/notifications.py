"""dbops.notifications — Notifications v2 and action-items mixin.

Owns: add/query/watermark notifications_v2 (session-scoped), add/dismiss
action_items.
Must not own: legacy notifications v1 table (schema only, no business logic),
thread state, agent pool.
"""

from __future__ import annotations

from datetime import datetime, timezone


class NotificationsMixin:
    """Mixin for notifications_v2 and action_items operations."""

    # ---------------------------------------------------------------
    # Notifications v2
    # ---------------------------------------------------------------

    def emit_event(
        self,
        thread_id,
        message: str,
        session_id: str,
        kind: str = "legacy",
        handled_by: str | None = None,
    ) -> int:
        """Insert a notifications_v2 row tagged with kind/handled_by. Returns new id.

        handled_by defaults to the routing derived from ``kind`` (see
        dbops.event_kinds) — pass it explicitly only to override.
        """
        if handled_by is None:
            from dbops.event_kinds import handled_by_for_kind

            handled_by = handled_by_for_kind(kind)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO notifications_v2 "
                "(thread_id, message, created_at, session_id, kind, handled_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (thread_id, message, now, session_id, kind, handled_by),
            )
            conn.commit()
            return cur.lastrowid

    def add_notification_v2(self, thread_id, message: str, session_id: str) -> int:
        """Insert a notifications_v2 row. Returns new id.

        Routes through emit_event() with kind='legacy' — behavior-preserving
        seam for callers not yet converted to real kinds (see T1a).
        """
        return self.emit_event(thread_id, message, session_id)

    def get_notifications_for_session(self, session_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, thread_id, message, created_at, session_id "
                "FROM notifications_v2 WHERE session_id = ? ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_notifications_last_n(self, session_id: str, n: int = 5) -> list[dict]:
        """Return the n most-recent notifications for session_id (newest-first)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, thread_id, message, created_at, session_id "
                "FROM notifications_v2 WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, n),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_notifications_since_id(self, session_id: str, last_id: int) -> list[dict]:
        """Return notifications with id > last_id for session_id (oldest-first)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, thread_id, message, created_at, session_id "
                "FROM notifications_v2 WHERE session_id = ? AND id > ? "
                "ORDER BY id ASC",
                (session_id, last_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_notif_watermark(self, claude_session_id: str) -> int | None:
        """Return the last-seen notification ID for this Claude session, or None if unseen."""
        with self._connect() as conn:
            val = self._get_session_key(conn, f"notif_watermark:{claude_session_id}")
        return int(val) if val is not None else None

    def set_notif_watermark(self, claude_session_id: str, last_id: int) -> None:
        """Record the last-seen notification ID for this Claude session."""
        with self._connect() as conn:
            self._set_session_key(
                conn, f"notif_watermark:{claude_session_id}", str(last_id)
            )

    def clear_notifications_v2_for_other_sessions(
        self, current_session_id: str
    ) -> int:
        """Delete notifications_v2 whose session_id != current. Returns rows deleted."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM notifications_v2 WHERE session_id != ?",
                (current_session_id,),
            )
            conn.commit()
            return cur.rowcount

    # ---------------------------------------------------------------
    # Action items
    # ---------------------------------------------------------------

    def add_action_item(
        self, thread_id, message: str, type_: str, priority: str = "normal"
    ) -> int:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO action_items (thread_id, message, type, priority, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (thread_id, message, type_, priority, now),
            )
            conn.commit()
            return cur.lastrowid

    def add_action_item_once(
        self, thread_id, message: str, type_: str, priority: str = "normal"
    ):
        """add_action_item, but skip when an OPEN item already carries the same
        thread+message signature (fix-conflict-envelope-routing, 2026-07-04): a
        re-driven integrate gate must not file byte-identical HIGH items every
        backoff round (~25 tonight, 5312-5340). Returns the new/existing id."""
        for item in self.get_open_action_items():
            if item.get("thread_id") == thread_id and item.get("message") == message:
                return item["id"]
        return self.add_action_item(thread_id, message, type_, priority)

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

    def dismiss_action_items_for_thread(self, thread_id: str) -> int:
        """Dismiss all open action items for thread_id. Returns count dismissed."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE action_items SET dismissed_at = ? WHERE thread_id = ? AND dismissed_at IS NULL",
                (now, thread_id),
            )
            conn.commit()
            return cursor.rowcount

    def dismiss_orphan_action_items(self) -> int:
        """Dismiss all open action items with thread_id IS NULL. Returns count dismissed."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE action_items SET dismissed_at = ? WHERE thread_id IS NULL AND dismissed_at IS NULL",
                (now,),
            )
            conn.commit()
            return cursor.rowcount
