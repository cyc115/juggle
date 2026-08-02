"""Juggle-DB reads for the /schedule:dogfood routine.

Extracted from schedules.dogfood (loc-gate budget, 2026-08-02): the pre-flight
gate queries that decide whether a dogfood run may proceed, plus the resolver for
the thread its action item is filed against. Pure move, no behaviour change.

Every read here is defensive on purpose. dogfood runs unattended (cron, and in a
cloud container where juggle.db may be fresh, absent, or on an older schema), so
a failed query must degrade to "no signal" rather than crash the routine.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from schedules.common import db_query

logger = logging.getLogger(__name__)


def _check_prior_dogfood_thread(db) -> str | None:
    """Return open prior dogfood thread id if any, else None."""
    try:
        # P8 Task 3.1: conversations read from nodes; topic->title, status->state
        # (terminal closed/archived/failed -> done/archived/failed-exec, bijective).
        rows = db_query(
            db,
            "SELECT id, title FROM nodes WHERE kind='conversation' "
            "AND title LIKE 'dogfood-%' "
            "AND state NOT IN ('done','archived','failed-exec')"
        )
        if rows:
            return rows[0]["title"]
    except Exception as e:
        logger.warning("prior dogfood thread check failed: %s", e)
    return None


def _check_active_session(db) -> bool:
    """Return True if Juggle session actively in use in last 30 min."""
    try:
        # P8 Task 3.1: live conversations read from nodes; status='active'->state='open'.
        rows = db_query(
            db,
            "SELECT last_active_at FROM nodes WHERE kind='conversation' "
            "AND state = 'open' ORDER BY last_active_at DESC LIMIT 1"
        )
        if not rows:
            return False
        last_active = rows[0].get("last_active_at") or ""
        if not last_active:
            return False
        dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
        age_secs = (datetime.now(timezone.utc) - dt).total_seconds()
        return age_secs < 1800
    except Exception:
        return False


def _find_or_create_schedule_thread(db) -> str | None:
    """Return id of a schedule-related thread, or None."""
    try:
        # P8 Task 3.1: conversations read from nodes (title<-topic).
        rows = db_query(
            db,
            "SELECT id FROM nodes WHERE kind='conversation' "
            "AND title LIKE 'schedule%' LIMIT 1")
        if rows:
            return rows[0]["id"]
        rows = db_query(
            db,
            "SELECT id FROM nodes WHERE kind='conversation' "
            "ORDER BY created_at DESC LIMIT 1")
        if rows:
            return rows[0]["id"]
    except Exception:
        pass
    return None
