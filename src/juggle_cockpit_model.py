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


def snapshot(db) -> CockpitState:
    """Read DB state into a frozen CockpitState. Only function that touches DB."""
    conn = db._connect()

    # --- Session: current thread id ---
    row = conn.execute(
        "SELECT value FROM session WHERE key = 'current_thread'"
    ).fetchone()
    current_thread_id: str | None = row[0] if row else None

    # --- Threads → Topics + Actions ---
    thread_rows = conn.execute(
        """
        SELECT id, label, status, last_active, open_questions,
               agent_result, show_in_list, reviewed, title, topic
        FROM threads
        WHERE show_in_list = 1 AND status != 'archived'
        ORDER BY created_at
        """
    ).fetchall()

    topics: list[Topic] = []
    actions: list[Action] = []

    for row in thread_rows:
        tid = row[0]
        label = row[1] or "?"
        status = row[2] or "active"
        last_active = row[3]
        open_questions_raw = row[4] or "[]"
        agent_result = row[5]
        reviewed = bool(row[7])
        title = row[8] or row[9] or "?"

        age = _age_secs(last_active)
        is_current = (tid == current_thread_id)

        topics.append(Topic(
            id=tid,
            label=label,
            status=status,
            age_secs=age,
            is_current=is_current,
            title=title,
        ))

        # BLOCKER action
        result_str = agent_result or ""
        if result_str.startswith("⚠️ BLOCKER:"):
            blocker_text = result_str[len("⚠️ BLOCKER:"):].strip()
            actions.append(Action(
                id=f"blocker:{tid}",
                topic_id=label,
                text=blocker_text,
                tier=TIER_BLOCKER,
                age_secs=age,
            ))

        # REVIEW nudge
        if status == "done" and result_str and not is_current and not reviewed:
            actions.append(Action(
                id=f"review:{tid}",
                topic_id=label,
                text="agent finished — results ready",
                tier=TIER_REVIEW,
                age_secs=age,
            ))

        # Open questions
        try:
            oq_list = json.loads(open_questions_raw)
        except (json.JSONDecodeError, TypeError):
            oq_list = []

        for i, oq in enumerate(oq_list):
            # oq may be a str or a dict — extract text robustly (fixes dict-repr leak)
            if isinstance(oq, dict):
                oq_text = oq.get("text") or oq.get("question") or str(oq)
            else:
                oq_text = str(oq)
            actions.append(Action(
                id=f"oq:{tid}:{i}",
                topic_id=label,
                text=oq_text,
                tier=TIER_REVIEW + 1,  # tier 2 = open question
                age_secs=age,
            ))

    # Sort actions: tier asc, age desc
    actions.sort(key=lambda a: (a.tier, -a.age_secs))

    # --- Agents ---
    agent_rows = conn.execute(
        """
        SELECT id, role, assigned_thread, status, last_active
        FROM agents
        ORDER BY created_at
        """
    ).fetchall()

    agents: list[Agent] = []
    for row in agent_rows:
        a_id = row[0] or ""
        role = row[1] or "unknown"
        assigned_thread = row[2]
        a_status_raw = row[3] or "idle"
        a_last_active = row[4]

        if a_status_raw == "busy":
            display_status = "busy"
        elif a_status_raw == "decommission_pending":
            display_status = "stale"
        else:
            display_status = "idle"

        # Resolve thread label from UUID
        topic_label: str | None = None
        if assigned_thread:
            for t_row in thread_rows:
                if t_row[0] == assigned_thread:
                    topic_label = t_row[1]
                    break

        agents.append(Agent(
            id_short=a_id[:8],
            role=role,
            status=display_status,
            topic_id=topic_label,
            age_secs=_age_secs(a_last_active),
        ))

    # --- Notifications (non-action severity, newest first, max 8) ---
    notif_rows = conn.execute(
        """
        SELECT n.message, n.severity, n.created_at
        FROM notifications n
        WHERE n.severity != 'action'
        ORDER BY n.id DESC
        LIMIT 8
        """
    ).fetchall()

    notifications: list[Notification] = []
    for row in notif_rows:
        msg = row[0] or ""
        severity = row[1] or "info"
        n_age = _age_secs(row[2])
        notifications.append(Notification(
            text=msg,
            kind=severity,
            age_secs=n_age,
        ))

    return CockpitState(
        topics=topics,
        actions=actions,
        agents=agents,
        notifications=notifications,
        fetched_at=time.time(),
    )
