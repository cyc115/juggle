"""juggle_spool_journal — spool_journal idempotency-ledger helpers (T-spool).

Extracted verbatim from juggle_spool_apply (2026-07-05, LOC gate) so the apply
module stays under budget. Owns ONLY the tiny read/write wrappers around the
``spool_journal`` table used by apply_event's journal-first idempotency:
read the current outcome, UPSERT to 'applying' before a handler runs, and set a
terminal outcome after. No apply logic, no dispatch — those stay in
juggle_spool_apply.
"""
from __future__ import annotations

from datetime import datetime, timezone


def journal_state(db, uuid: str) -> str | None:
    with db._connect() as conn:
        row = conn.execute(
            "SELECT outcome FROM spool_journal WHERE uuid = ?", (uuid,)
        ).fetchone()
    return row["outcome"] if row else None


def journal_insert_applying(db, uuid: str, event_type: str) -> None:
    # UPSERT to 'applying' (not INSERT OR IGNORE): a retried event keeps its
    # prior 'failed' row, but this attempt reruns the handler, so the row must
    # read 'applying' for the duration — else a mid-retry crash leaves 'failed'
    # and the next drain blind-retries a half-applied write (DA Res #2). Only
    # None/'failed' rows reach here (earlier guards short-circuit the rest).
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO spool_journal(uuid, event_type, applied_at, outcome) "
            "VALUES (?, ?, ?, 'applying') "
            "ON CONFLICT(uuid) DO UPDATE SET outcome='applying', applied_at=excluded.applied_at",
            (uuid, event_type, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def journal_set_outcome(db, uuid: str, outcome: str) -> None:
    with db._connect() as conn:
        conn.execute(
            "UPDATE spool_journal SET outcome = ?, applied_at = ? WHERE uuid = ?",
            (outcome, datetime.now(timezone.utc).isoformat(), uuid),
        )
        conn.commit()
