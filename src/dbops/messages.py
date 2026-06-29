"""dbops.messages — Message storage and context-window query mixin.

Owns: add_message, get_messages (token-budget windowing), get_message_count,
get_last_exchange, get_recent_exchanges.
Must not own: thread status, notifications, agent pool.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from dbops.schema import _get_settings, _is_junk_message, is_auto_topic_eligible


class MessagesMixin:
    """Mixin for per-thread message CRUD and context-window queries."""

    def get_messages(
        self, thread_id: str, token_budget: int | None = None
    ) -> list[dict]:
        """
        Load messages newest-first until token budget is exhausted
        (token estimate = len(content) // 4), then return in chronological order.
        """
        budget: int = (
            token_budget
            if token_budget is not None
            else int(_get_settings()["message_history_token_budget"])
        )
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
            # Keep the conversation node's last_active_at fresh. P8 c4-write-cut:
            # nodes is the sole conversation store — the legacy threads.last_active
            # write is gone.
            try:
                conn.execute(
                    "UPDATE nodes SET last_active_at = ? WHERE id = ? AND kind='conversation'",
                    (now, thread_id),
                )
            except sqlite3.OperationalError as e:
                # Missing nodes TABLE (pre-Migration-44) tolerated; a missing
                # COLUMN is a real schema gap and must FAIL LOUD (H4).
                if "no such table" not in str(e).lower():
                    raise
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

    def has_human_user_message(self, thread_id: str) -> bool:
        """True if the thread has ≥1 real human-authored user message.

        Excludes BOTH junk (task-notifications, slash commands — _is_junk_message)
        AND orchestrator chatter (autopilot cards, '# Autonomous loop tick'
        headers, JUGGLE ACTIVE blocks — is_auto_topic_eligible). The latter
        accumulate on ANY thread that was 'current' during loop ticks, so a plain
        user-message count would mis-flag a finished agent/orchestrator thread as
        a feature topic. Uses the same canonical classifier that gates auto-topic
        creation, so a feature topic (seeded by an eligible human message) reads
        as human-owned while an agent-owned ephemeral thread does not.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content FROM messages WHERE thread_id = ? AND role = 'user'",
                (thread_id,),
            ).fetchall()
        return any(
            not _is_junk_message(r["content"]) and is_auto_topic_eligible(r["content"])
            for r in rows
        )

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
                result["last_assistant_at"] = thread.get("last_active_at")

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
            row
            for row in all_rows
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
