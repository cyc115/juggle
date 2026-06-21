"""dbops.selfheal — Self-heal error_events mixin for JuggleDB.

Owns: dedup_or_insert_error, set_error_event_status, get_open_error_events,
get_pending_selfheal_count.
Must not own: action-item creation (delegated to NotificationsMixin),
agent pool, thread operations.
"""

from __future__ import annotations

from datetime import datetime, timezone

from dbops.schema import VALID_ERROR_STATUSES


class SelfhealMixin:
    """Mixin for self-heal error_events table operations."""

    def dedup_or_insert_error(
        self,
        signature_hash: str,
        error_class: str,
        exc_type: str | None,
        traceback: str | None,
        entrypoint: str | None,
        command_args: str,
        surface: str | None = None,
        juggle_ref: str | None = None,
    ) -> int | None:
        """Insert new error_events row or increment count on duplicate.

        Returns new row id on INSERT; None on dedup (existing open/in-progress row).
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            # selfheal-triage-v2 P1 asymmetry (spec §4.1): match any LIVE row
            # (status != 'resolved') so a non_issue recurrence DEDUPS (sticky:
            # bump count, stay hidden) while a resolved recurrence MISSES and
            # inserts a fresh 'open' row (non-sticky: regression re-alert). The
            # partial unique index idx_error_events_sig mirrors this predicate.
            existing = conn.execute(
                "SELECT id FROM error_events "
                "WHERE signature_hash = ? AND status != 'resolved'",
                (signature_hash,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE error_events SET count = count + 1, last_seen = ? WHERE id = ?",
                    (now, existing["id"]),
                )
                conn.commit()
                return None
            cur = conn.execute(
                "INSERT INTO error_events "
                "(signature_hash, error_class, exc_type, traceback, entrypoint, "
                "surface, command_args, juggle_ref, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    signature_hash,
                    error_class,
                    exc_type,
                    traceback,
                    entrypoint,
                    surface,
                    command_args,
                    juggle_ref,
                    now,
                    now,
                ),
            )
            conn.commit()
            return cur.lastrowid

    def set_error_event_status(
        self,
        event_id: int,
        status: str,
        action_item_id: int | None = None,
    ) -> bool:
        """Update status (and optionally action_item_id) for an error_events row.

        Returns True if a row was updated.
        """
        if status not in VALID_ERROR_STATUSES:
            raise ValueError(
                f"invalid error_event status {status!r}; "
                f"valid: {sorted(VALID_ERROR_STATUSES)}"
            )
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            if action_item_id is not None:
                cur = conn.execute(
                    "UPDATE error_events SET status = ?, action_item_id = ?, last_seen = ? WHERE id = ?",
                    (status, action_item_id, now, event_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE error_events SET status = ?, last_seen = ? WHERE id = ?",
                    (status, now, event_id),
                )
            conn.commit()
            return cur.rowcount == 1

    def get_open_error_events(
        self, status: str | None = None, include_hidden: bool = False
    ) -> list[dict]:
        """Return error_events rows for the triage view.

        Default (status=None, include_hidden=False): the actionable view —
        status NOT IN ('resolved','non_issue'). So open/diagnosing/
        awaiting_approval/non_issue_proposed surface (the last greyed by the
        caller). include_hidden=True returns all rows; status='X' filters to
        exactly that status (selfheal-triage-v2 P1, spec §4.2).
        """
        with self._connect() as conn:
            if status is not None:
                rows = conn.execute(
                    "SELECT * FROM error_events WHERE status = ? ORDER BY id ASC",
                    (status,),
                ).fetchall()
            elif include_hidden:
                rows = conn.execute(
                    "SELECT * FROM error_events ORDER BY id ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM error_events "
                    "WHERE status NOT IN ('resolved','non_issue') ORDER BY id ASC"
                ).fetchall()
        return [dict(r) for r in rows]

    def get_pending_selfheal_count(self) -> int:
        """Count actionable (non-resolved, non-non_issue) error_events rows.

        Mirrors the default list view so the cockpit badge counts what an
        operator would act on (selfheal-triage-v2 P1).
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM error_events "
                    "WHERE status NOT IN ('resolved','non_issue')"
                ).fetchone()
                return row[0] if row else 0
        except Exception:
            return 0
