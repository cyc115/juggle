"""Juggle Cockpit Model — DB reads → typed frozen dataclasses. Zero Rich imports."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Topic:
    id: str            # thread UUID
    label: str         # "K"
    status: str        # "current" | "running" | "paused" | "done" | "failed" | "archived"
    age_secs: int
    is_current: bool
    title: str = ""    # display title


@dataclass(frozen=True)
class Action:
    id: str            # source identifier (open_questions item id or notifications row id)
    topic_id: str      # thread label e.g. "K"
    text: str          # display text — never a dict repr
    tier: int          # 0=blocker, 1=review, 2=question, 3=nudge
    age_secs: int


@dataclass(frozen=True)
class Agent:
    id_short: str      # first 8 chars of UUID
    role: str          # "coder" | "planner" | "researcher"
    status: str        # "busy" | "idle" | "stale"
    topic_id: str | None   # assigned thread label or None
    age_secs: int


@dataclass(frozen=True)
class Notification:
    text: str
    kind: str          # "complete" | "failed" | "info" | "warning" | "error"
    age_secs: int


@dataclass(frozen=True)
class CockpitState:
    topics: list[Topic]
    actions: list[Action]
    agents: list[Agent]
    notifications: list[Notification]
    fetched_at: float


# ---------------------------------------------------------------------------
# format_age
# ---------------------------------------------------------------------------

def format_age(secs: int | None) -> str:
    """Convert seconds to compact age string: '12s', '5m', '2h', '3d'."""
    if secs is None:
        return "—"
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# ---------------------------------------------------------------------------
# priority_tier
# ---------------------------------------------------------------------------

TIER_BLOCKER = 0
TIER_REVIEW = 1
TIER_BACKGROUND = 2
TIER_CURRENT = 3
TIER_WAITING = 4
TIER_IDLE = 5
TIER_DONE = 6

_IDLE_THRESHOLD_SECS = 2 * 3600  # 2 hours


def priority_tier(
    agent_result: str | None,
    status: str,
    last_active_age_secs: int | None,
    is_current: bool,
    reviewed: bool = False,
) -> int:
    """Compute display-priority tier for a thread. Lower = higher priority."""
    result = agent_result or ""

    if result.startswith("⚠️ BLOCKER:"):
        return TIER_BLOCKER

    if status == "done" and result and not is_current and not reviewed:
        return TIER_REVIEW

    if status == "background":
        return TIER_BACKGROUND

    if is_current:
        return TIER_CURRENT

    if last_active_age_secs is not None and last_active_age_secs > _IDLE_THRESHOLD_SECS:
        return TIER_IDLE

    if status == "done":
        return TIER_DONE

    return TIER_IDLE


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

def _age_secs(last_active: str | None) -> int:
    """Return seconds since last_active ISO timestamp, or 0 if unparseable."""
    if not last_active:
        return 0
    try:
        dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - dt).total_seconds()
        return max(0, int(delta))
    except (ValueError, TypeError):
        return 0


_ARCHIVED_DISPLAY_LIMIT = 10  # spec: most-recent N, default 10


def snapshot(db) -> CockpitState:
    """Read DB state into a frozen CockpitState. Only function that touches DB."""
    import json as _json
    from datetime import datetime, timezone, timedelta

    conn = db._connect()

    # --- Session ---
    row = conn.execute(
        "SELECT value FROM session WHERE key = 'current_thread'"
    ).fetchone()
    current_thread_id: str | None = row[0] if row else None

    row = conn.execute(
        "SELECT value FROM session WHERE key = 'session_id'"
    ).fetchone()
    session_id = row[0] if row else ""

    # TTL for closed threads
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'thread_auto_archive_ttl_secs'"
        ).fetchone()
        ttl_secs = int(row[0]) if row else 3600
    except Exception:
        ttl_secs = 3600

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_secs)
    cutoff_s = cutoff.strftime("%Y-%m-%d %H:%M")

    topics: list[Topic] = []

    def _make_topic(r) -> Topic:
        # support both sqlite3.Row and dict
        def _get(key, default=None):
            try:
                return r[key] if r[key] is not None else default
            except (IndexError, KeyError):
                return default

        tid = _get("id", "?")
        label = _get("user_label") or _get("label") or (tid[:6] if tid else "?")
        title = _get("title") or _get("topic") or "?"
        status = _get("status") or "active"
        age = _age_secs(_get("last_active_at") or _get("last_active"))
        return Topic(
            id=tid,
            label=label,
            status=status,
            age_secs=age,
            is_current=(tid == current_thread_id),
            title=title,
        )

    # 1. Active
    for r in conn.execute(
        "SELECT * FROM threads WHERE status = 'active' ORDER BY last_active_at DESC"
    ).fetchall():
        topics.append(_make_topic(r))

    # 2. Running
    for r in conn.execute(
        "SELECT * FROM threads WHERE status = 'running' ORDER BY last_active_at DESC"
    ).fetchall():
        topics.append(_make_topic(r))

    # 3. Closed within TTL
    for r in conn.execute(
        "SELECT * FROM threads WHERE status = 'closed' "
        "AND last_active_at >= ? ORDER BY last_active_at DESC",
        (cutoff_s,),
    ).fetchall():
        topics.append(_make_topic(r))

    # 4. Archived, most recent N
    for r in conn.execute(
        "SELECT * FROM threads WHERE status = 'archived' "
        "ORDER BY last_active_at DESC LIMIT ?",
        (_ARCHIVED_DISPLAY_LIMIT,),
    ).fetchall():
        topics.append(_make_topic(r))

    # --- Actions ← action_items table ---
    _PRIO_TIER = {"high": 0, "normal": 1, "low": 2}
    action_rows = conn.execute(
        """
        SELECT id, thread_id, message, type, priority, created_at
        FROM action_items
        WHERE dismissed_at IS NULL
        ORDER BY
          CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
          created_at DESC
        """
    ).fetchall()

    actions: list[Action] = []
    for r in action_rows:
        topic_label = ""
        if r["thread_id"]:
            t_row = conn.execute(
                "SELECT user_label, id FROM threads WHERE id = ?",
                (r["thread_id"],),
            ).fetchone()
            if t_row:
                topic_label = t_row["user_label"] or (t_row["id"] or "")[:6]
        actions.append(Action(
            id=f"ai:{r['id']}",
            topic_id=topic_label,
            text=r["message"],
            tier=_PRIO_TIER.get(r["priority"], 1),
            age_secs=_age_secs(r["created_at"]),
        ))

    # --- Agents (unchanged) ---
    agent_rows = conn.execute(
        """
        SELECT id, role, assigned_thread, status, last_active
        FROM agents ORDER BY created_at
        """
    ).fetchall()

    agents: list[Agent] = []
    for r in agent_rows:
        a_id = r["id"] or ""
        role = r["role"] or "unknown"
        a_status_raw = r["status"] or "idle"
        if a_status_raw == "busy":
            display_status = "busy"
        elif a_status_raw == "decommission_pending":
            display_status = "stale"
        else:
            display_status = "idle"
        topic_label_a: str | None = None
        if r["assigned_thread"]:
            tr = conn.execute(
                "SELECT user_label, id FROM threads WHERE id = ?",
                (r["assigned_thread"],),
            ).fetchone()
            if tr:
                topic_label_a = tr["user_label"] or (tr["id"] or "")[:6]
        agents.append(Agent(
            id_short=a_id[:8],
            role=role,
            status=display_status,
            topic_id=topic_label_a,
            age_secs=_age_secs(r["last_active"]),
        ))

    # --- Notifications ← notifications_v2 for current session ---
    try:
        notif_rows = conn.execute(
            """
            SELECT message, created_at FROM notifications_v2
            WHERE session_id = ? ORDER BY id DESC LIMIT 20
            """,
            (session_id,),
        ).fetchall()
        notifications: list[Notification] = [
            Notification(text=r["message"], kind="info", age_secs=_age_secs(r["created_at"]))
            for r in notif_rows
        ]
    except Exception:
        notifications = []

    return CockpitState(
        topics=topics,
        actions=actions,
        agents=agents,
        notifications=notifications,
        fetched_at=time.time(),
    )
