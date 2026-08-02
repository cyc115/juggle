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

# Snapshot budget. The block is interpolated into the research prompt, so it is
# hard-capped rather than trusted to stay small on a busy week.
SNAPSHOT_CHAR_CAP = 2000
NO_DB_DATA = (
    "_No DB data available for this window — juggle.db is empty, absent, or on an "
    "older schema. Say so in the report; do NOT infer activity from git history._"
)
_TRUNCATED = "\n… (snapshot truncated)"


def _extract_db_snapshot(db, since_date: str, char_cap: int = SNAPSHOT_CHAR_CAP) -> str:
    """Compact markdown digest of the Juggle DB for the report window.

    Never raises. Each section is probed independently so a missing table (fresh
    cloud container, pre-migration DB) costs only that section; if nothing at all
    is readable, returns NO_DB_DATA so the prompt states the blindness explicitly
    instead of silently omitting it — the failure mode this exists to end.
    """
    since = str(since_date or "")
    lines: list[str] = []
    for heading, probe in (
        ("Nodes (threads/tasks active in window)", _snapshot_nodes),
        ("Agent completions", _snapshot_completions),
        ("Watchdog events", _snapshot_watchdog),
        ("Action items", _snapshot_action_items),
    ):
        try:
            rendered = probe(db, since)
        except Exception as e:  # missing table / unreadable DB / bad handle
            logger.warning("dogfood snapshot section %r unavailable: %s", heading, e)
            continue
        if rendered:
            lines.append(f"**{heading}**")
            lines.extend(rendered)
            lines.append("")
    if not lines:
        return NO_DB_DATA
    return _cap("\n".join(lines).rstrip(), char_cap)


def _cap(text: str, char_cap: int) -> str:
    """Clamp to char_cap on a line boundary; the marker fits INSIDE the cap."""
    if len(text) <= char_cap:
        return text
    if char_cap <= len(_TRUNCATED):        # cap too small to hold the marker
        return text[:char_cap]
    return text[: char_cap - len(_TRUNCATED)].rsplit("\n", 1)[0] + _TRUNCATED


def _snapshot_nodes(db, since: str) -> list[str]:
    rows = db_query(
        db,
        "SELECT kind, state, COUNT(*) AS n FROM nodes "
        "WHERE COALESCE(updated_at, created_at) >= ? "
        "GROUP BY kind, state ORDER BY n DESC, kind, state LIMIT 20",
        (since,),
    )
    out = [f"- {r['kind']}/{r['state']}: {r['n']}" for r in rows]
    recent = db_query(
        db,
        "SELECT title, state FROM nodes "
        "WHERE COALESCE(updated_at, created_at) >= ? "
        "ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 12",
        (since,),
    )
    if recent:
        out.append("- recent: " + "; ".join(
            f"{(r['title'] or '?')[:48]} [{r['state']}]" for r in recent))
    return out


def _snapshot_completions(db, since: str) -> list[str]:
    rows = db_query(
        db,
        "SELECT role, COUNT(*) AS n, ROUND(AVG(duration_secs), 1) AS avg_s, "
        "ROUND(MAX(duration_secs), 1) AS max_s FROM agent_completions "
        "WHERE completed_at >= ? GROUP BY role ORDER BY n DESC LIMIT 10",
        (since,),
    )
    return [f"- {r['role']}: {r['n']} runs, avg {r['avg_s']}s, max {r['max_s']}s"
            for r in rows]


def _snapshot_watchdog(db, since: str) -> list[str]:
    rows = db_query(
        db,
        "SELECT event_type, COUNT(*) AS n FROM watchdog_events "
        "WHERE created_at >= ? GROUP BY event_type ORDER BY n DESC LIMIT 12",
        (since,),
    )
    return [f"- {r['event_type']}: {r['n']}" for r in rows]


def _snapshot_action_items(db, since: str) -> list[str]:
    rows = db_query(
        db,
        "SELECT type, priority, COUNT(*) AS n, "
        "SUM(CASE WHEN dismissed_at IS NULL THEN 1 ELSE 0 END) AS still_open "
        "FROM action_items WHERE created_at >= ? "
        "GROUP BY type, priority ORDER BY n DESC LIMIT 12",
        (since,),
    )
    return [f"- {r['type']}/{r['priority']}: {r['n']} ({r['still_open']} still open)"
            for r in rows]


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
