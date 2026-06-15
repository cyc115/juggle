"""dbops.threads — Thread CRUD, state machine, archive, and stale-query mixin.

Owns: create/get/update/list threads, thread status transitions, archive/
unarchive, stale-thread detection, and archive-candidate selection.
Must not own: message content, project assignment, agent pool, notifications.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

import dbops.schema as _schema
from dbops.schema import (
    _get_settings,
    _is_junk_message,
    _next_excel_label,
    _thread_age_seconds,
)

# Read MAX_THREADS via module reference so tests can patch dbops.threads.MAX_THREADS
# (or dbops.schema.MAX_THREADS) to bypass the cap in seeding fixtures.
MAX_THREADS = _schema.MAX_THREADS


class ThreadsMixin:
    """Mixin for thread CRUD, state machine, archive ops, and stale detection."""

    # ---------------------------------------------------------------
    # Thread CRUD
    # ---------------------------------------------------------------

    def create_thread(self, topic: str, session_id: str) -> str:
        """Create a new thread. Returns the UUID of the new thread.

        Assigns next available A–Z label. Raises ValueError if 10 non-archived
        threads already exist or all 26 labels are in use.
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT id, status FROM threads").fetchall()
            active_count = sum(1 for row in rows if row["status"] != "archived")
            if active_count >= MAX_THREADS:  # noqa: SIM102 — patched by test fixtures
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
            used_labels = {
                row["user_label"]
                for row in conn.execute(
                    "SELECT user_label FROM threads WHERE user_label IS NOT NULL"
                    " AND status NOT IN ('archived', 'closed')"
                ).fetchall()
            }
            user_label = _next_excel_label(used_labels)
            now_iso = datetime.now(timezone.utc).isoformat()
            now_min = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            # Retry up to 3 times in case a stale archived/closed row still
            # physically holds the chosen label (pre-fix accumulation).
            for _attempt in range(3):
                try:
                    conn.execute(
                        """
                        INSERT INTO threads
                          (id, user_label, session_id, topic, status,
                           key_decisions, open_questions,
                           last_user_intent, agent_task_id, agent_result,
                           show_in_list, summarized_msg_count, created_at, last_active, last_active_at)
                        VALUES (?, ?, ?, ?, 'active', '[]', '[]', '', NULL, NULL, 1, 0, ?, ?, ?)
                        """,
                        (new_id, user_label, session_id, topic, now_iso, now_iso, now_min),
                    )
                    conn.commit()
                    return new_id
                except sqlite3.IntegrityError as exc:
                    if "user_label" not in str(exc):
                        raise
                    # Stale archived/closed row holds this label; clear it and retry.
                    conn.execute(
                        "UPDATE threads SET user_label = NULL"
                        " WHERE user_label = ? AND status IN ('archived', 'closed')",
                        (user_label,),
                    )
            raise RuntimeError(
                f"create_thread: could not assign label {user_label!r} after retries"
            )

    def get_thread(self, thread_id: str) -> dict | None:
        """Look up a thread by its UUID `id`. Returns None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if row is None:
                return None
            return dict(row)

    def get_thread_by_user_label(self, label: str | None) -> dict | None:
        """Look up a thread by its user_label (e.g. 'A', 'BC'). Case-insensitive.

        Prefers non-archived/closed threads so that recycled labels resolve to
        the current active holder, not the archived original.
        Returns None if label is None or not found.
        """
        if not label:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM threads WHERE user_label = ? ORDER BY "
                "CASE WHEN status NOT IN ('archived', 'closed') THEN 0 ELSE 1 END, "
                "CASE WHEN length(id) = 36 AND id LIKE '%-%-%-%-%' THEN 0 ELSE 1 END, "
                "last_active_at DESC LIMIT 1",
                (label.upper(),),
            ).fetchone()
        return dict(row) if row else None

    def get_all_threads(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM threads ORDER BY created_at").fetchall()
            return [dict(row) for row in rows]

    def update_thread(self, thread_id: str, **kwargs):
        """Update any column(s) on a thread row."""
        import json

        if not kwargs:
            return
        # Recycle user_label when a thread is archived or closed so the label
        # becomes available for the next active thread.
        if kwargs.get("status") in ("archived", "closed"):
            kwargs.setdefault("user_label", None)
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

    # ---------------------------------------------------------------
    # Thread state machine
    # ---------------------------------------------------------------

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
            if status in ("archived", "closed"):
                conn.execute(
                    "UPDATE threads SET status = ?, user_label = NULL, "
                    "last_active_at = ? WHERE id = ?",
                    (status, now, thread_id),
                )
            else:
                conn.execute(
                    "UPDATE threads SET status = ?, last_active_at = ? WHERE id = ?",
                    (status, now, thread_id),
                )
            conn.commit()

    def touch_last_active(self, thread_id: str) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            conn.execute(
                "UPDATE threads SET last_active_at = ? WHERE id = ?",
                (now, thread_id),
            )
            conn.commit()

    def get_threads_by_status(self, status: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM threads WHERE status = ? ORDER BY last_active DESC",
                (status,),
            ).fetchall()
            return [dict(row) for row in rows]

    # ---------------------------------------------------------------
    # Archive operations
    # ---------------------------------------------------------------

    def archive_thread(self, thread_id: str):
        """Set status='archived', show_in_list=0. Clears user_label so it can
        be recycled by the next active thread."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            conn.execute(
                "UPDATE threads SET status = 'archived', "
                "show_in_list = 0, user_label = NULL, last_active_at = ? WHERE id = ?",
                (now, thread_id),
            )
            conn.commit()

    def unarchive_thread(self, thread_id: str) -> str:
        """Unarchive: status=active, show_in_list=1. Assigns a fresh label since
        the label was cleared on archive to allow recycling."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            used_labels = {
                row["user_label"]
                for row in conn.execute(
                    "SELECT user_label FROM threads WHERE user_label IS NOT NULL"
                    " AND status NOT IN ('archived', 'closed') AND id != ?"
                    , (thread_id,)
                ).fetchall()
            }
            new_label = _next_excel_label(used_labels)
            conn.execute(
                "UPDATE threads SET status = 'active', show_in_list = 1, "
                "user_label = ?, last_active_at = ? WHERE id = ?",
                (new_label, now, thread_id),
            )
            conn.commit()
        return new_label

    # ---------------------------------------------------------------
    # Stale / archive-candidate queries
    # ---------------------------------------------------------------

    def get_stale_threads(self, threshold: int | None = None) -> list[dict]:
        """Return threads where substantive user message delta >= threshold.

        Uses a single DB query for all threads instead of N per-thread calls.
        """
        limit: int = (
            threshold
            if threshold is not None
            else int(_get_settings()["stale_summary_message_threshold"])
        )
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
            if (
                age is not None
                and age > _get_settings()["thread_archive_threshold_secs"]
                and status not in ("background", "waiting")
            ):
                candidates.append(t)

        return candidates
