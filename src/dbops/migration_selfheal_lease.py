"""Migration 49 (selfheal-triage-v2 P2, 2026-06-21) — error_events.benign_until.

Additive: adds the set-once ``benign_until`` lease column (anchored at hide-time,
NOT a last_seen proxy) and BACKFILLS it for legacy ``non_issue`` rows so they
stop carrying the latent never-expiring-lease bug (DA fix d).

Idempotent: the ALTER is PRAGMA-guarded; the backfill only touches non_issue rows
whose benign_until IS NULL. Own module (mirrors mig 45) for the loc_gate budget.
"""
from __future__ import annotations

import logging
import sqlite3

# Default lease used for the legacy backfill; mirrors settings'
# `resurface_lease_days` default so legacy hides re-confirm on the same cadence.
_DEFAULT_LEASE_DAYS = 30

_log = logging.getLogger(__name__)


def migrate_selfheal_lease(conn: sqlite3.Connection) -> None:
    """Add error_events.benign_until and backfill legacy non_issue rows. Idempotent."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='error_events'"
    ).fetchone()
    if row is None:
        return  # fresh DB without the table yet — CREATE path already has the column
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(error_events)").fetchall()}
        if "benign_until" not in cols:
            conn.execute("ALTER TABLE error_events ADD COLUMN benign_until TEXT")
        # DA fix d: legacy non_issue rows have NULL benign_until and would never
        # lease out via the new set-once branch. Anchor their lease at first_seen
        # + default so old benign classifications re-confirm instead of sticking
        # forever.
        conn.execute(
            "UPDATE error_events "
            "SET benign_until = strftime('%Y-%m-%d %H:%M', datetime(first_seen, ?)) "
            "WHERE status='non_issue' AND benign_until IS NULL AND first_seen IS NOT NULL",
            (f"+{_DEFAULT_LEASE_DAYS} days",),
        )
        conn.commit()
        _log.info("Migration 49: error_events.benign_until added + legacy backfill")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 49 (benign_until) skipped: %s", e)
