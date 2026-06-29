"""Juggle Cockpit Model — DB reads → typed frozen dataclasses. Zero Rich imports."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from juggle_cockpit_sched import (  # noqa: F401 — re-exported model types
    ScheduledTask,
    fetch_scheduled_tasks,
)
from juggle_cockpit_graph_dag import (  # noqa: F401 — re-exported for back-compat
    GraphDag,
    load_graph_dags as _load_graph_dags,
)
from dbops.node_translation import status_for_state


@dataclass(frozen=True)
class Topic:
    id: str  # thread UUID
    label: str  # "K"
    status: str  # "current" | "running" | "paused" | "done" | "failed" | "archived"
    age_secs: int
    is_current: bool
    title: str = ""  # display title
    project_id: str = "INBOX"
    project_name: str = "Inbox"
    task_state: str | None = None  # bound graph task's state (autopilot), or None


@dataclass(frozen=True)
class Action:
    id: str  # source identifier (open_questions item id or notifications row id)
    topic_id: str  # thread label e.g. "K"
    text: str  # display text — never a dict repr
    tier: int  # 0=blocker, 1=review, 2=question, 3=nudge
    age_secs: int


@dataclass(frozen=True)
class Agent:
    id_short: str  # first 8 chars of UUID
    role: str  # "coder" | "planner" | "researcher"
    status: str  # "busy" | "idle" | "stale"
    topic_id: str | None  # assigned thread label or None
    age_secs: int
    pane_id: str | None = None  # tmux pane ID e.g. "%664"; None if missing
    harness: str | None = None  # harness adapter id e.g. "claude", "reasonix"
    model: str | None = None  # model name e.g. "sonnet", "deepseek-v4-pro"


@dataclass(frozen=True)
class Notification:
    text: str
    kind: str  # "complete" | "failed" | "info" | "warning" | "error"
    age_secs: int


@dataclass(frozen=True)
class CockpitState:
    topics: list[Topic]
    actions: list[Action]
    agents: list[Agent]
    notifications: list[Notification]
    scheduled: list[ScheduledTask]
    fetched_at: float
    projects_by_id: dict = None  # type: ignore  # {id: name}, None → no grouping
    graph_by_project: dict = None  # type: ignore  # {id: task counts}, None → no graph
    graph_dag: "GraphDag | None" = None  # armed-project DAG, only in graph mode
    graph_dags: "list | None" = None  # all armed projects' DAGs (multi-project)


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


def snapshot(db, *, load_graph_dag: bool = False) -> CockpitState:
    """Read DB state into a frozen CockpitState. Only function that touches DB.

    Opens a fresh SQLite connection each call so every snapshot sees the latest
    committed writes from any connection (avoids WAL read-transaction pinning).

    load_graph_dag: when True (graph mode active), ALSO load the armed project's
    DAG (tasks+edges) into ``graph_dag``. Default False → zero extra cost.
    """
    import sqlite3 as _sqlite3
    from datetime import datetime, timezone, timedelta

    # Open a fresh connection each call to avoid WAL read-transaction pinning.
    # Fall back to db._connect() for in-memory / fake DBs that expose no db_path.
    if hasattr(db, "db_path"):
        from juggle_db_connect import open_connection
        conn = open_connection(db.db_path)
    else:
        conn = db._connect()

    # --- Session ---
    row = conn.execute(
        "SELECT value FROM session WHERE key = 'current_thread'"
    ).fetchone()
    current_thread_id: str | None = row[0] if row else None

    row = conn.execute("SELECT value FROM session WHERE key = 'session_id'").fetchone()
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

    # Load projects for grouping (graceful fallback for old DBs without the table)
    try:
        proj_rows = conn.execute(
            "SELECT id, name FROM projects WHERE status != 'archived' ORDER BY (id='INBOX'), id"
        ).fetchall()
        projects_by_id: dict[str, str] = {r[0]: r[1] for r in proj_rows}
    except Exception:
        projects_by_id = {}

    # Graph-task visibility: aggregate counts per project sourced from the unified
    # nodes table (P8). Root graph units: topics (kind='topic') plus parentless
    # task/research nodes. Pre-migration DBs degrade gracefully.
    graph_by_project: dict | None = None
    try:
        from juggle_graph_status import counts_from_states

        n_rows = conn.execute(
            "SELECT project_id, state FROM nodes "
            "WHERE kind IN ('topic','task','research') AND parent_id IS NULL"
        ).fetchall()
        states_by_proj: dict[str, list[str]] = {}
        for r in n_rows:
            pid = r["project_id"] or "INBOX"
            states_by_proj.setdefault(pid, []).append(r["state"])
        if states_by_proj:
            graph_by_project = {
                pid: counts_from_states(states)
                for pid, states in states_by_proj.items()
            }
    except Exception:
        pass

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
        title = _get("title") or "?"
        # P8 Task 3.1: rows come from nodes; map node state -> legacy status vocab.
        status = status_for_state(_get("state") or "open")
        age = _age_secs(_get("last_active_at"))
        pid = _get("project_id") or "INBOX"
        pname = projects_by_id.get(pid, "Inbox")
        return Topic(
            id=tid,
            label=label,
            status=status,
            age_secs=age,
            is_current=(tid == current_thread_id),
            title=title,
            project_id=pid,
            project_name=pname,
            task_state=None,
        )

    # Conversation panels read nodes (kind='conversation'); status->state value-mapped (P8 Task 3.1).
    # 1. Active
    for r in conn.execute(
        "SELECT * FROM nodes WHERE kind = 'conversation' AND state = 'open' ORDER BY last_active_at DESC"
    ).fetchall():
        topics.append(_make_topic(r))

    # 2. Running
    for r in conn.execute(
        "SELECT * FROM nodes WHERE kind = 'conversation' AND state = 'running' ORDER BY last_active_at DESC"
    ).fetchall():
        topics.append(_make_topic(r))

    # 2b. Background
    for r in conn.execute(
        "SELECT * FROM nodes WHERE kind = 'conversation' AND state = 'background' ORDER BY last_active_at DESC"
    ).fetchall():
        topics.append(_make_topic(r))

    # 3. Closed within TTL
    for r in conn.execute(
        "SELECT * FROM nodes WHERE kind = 'conversation' AND state = 'done' AND last_active_at >= ? ORDER BY last_active_at DESC",
        (cutoff_s,),
    ).fetchall():
        topics.append(_make_topic(r))

    # 4. Archived, most recent N
    for r in conn.execute(
        "SELECT * FROM nodes WHERE kind = 'conversation' AND state = 'archived' ORDER BY last_active_at DESC LIMIT ?",
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
        topic_label = "Z"  # sentinel for orphaned (thread_id IS NULL) items
        if r["thread_id"]:
            t_row = conn.execute(
                "SELECT user_label, id FROM nodes WHERE id = ? AND kind = 'conversation'",
                (r["thread_id"],),
            ).fetchone()
            if t_row:
                topic_label = t_row["user_label"] or (t_row["id"] or "")[:6]
        actions.append(
            Action(
                id=f"ai:{r['id']}",
                topic_id=topic_label,
                text=r["message"],
                tier=_PRIO_TIER.get(r["priority"], 1),
                age_secs=_age_secs(r["created_at"]),
            )
        )

    # --- Agents ---
    # Self-heal: reconcile stale busy one-shot agents before building the view.
    try:
        from juggle_tmux import reconcile_oneshot_agents
        reconcile_oneshot_agents(db)
    except Exception:
        pass

    agent_rows = conn.execute(
        """
        SELECT id, role, assigned_thread, status, last_active, pane_id, harness, model, busy_since
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
                "SELECT user_label, id FROM nodes WHERE id = ? AND kind = 'conversation'",
                (r["assigned_thread"],),
            ).fetchone()
            if tr:
                topic_label_a = tr["user_label"] or (tr["id"] or "")[:6]
        # Age: busy agents use busy_since; idle/others use last_active.
        age_ts = r["busy_since"] if display_status == "busy" and r["busy_since"] else r["last_active"]
        agents.append(
            Agent(
                id_short=a_id[:8],
                role=role,
                status=display_status,
                topic_id=topic_label_a,
                age_secs=_age_secs(age_ts),
                pane_id=r["pane_id"] or None,
                harness=r["harness"] or None,
                model=r["model"] or None,
            )
        )

    # --- Notifications ← notifications_v2 for current session ---
    try:
        notif_rows = conn.execute(
            """
            SELECT n.message, n.created_at, t.user_label
            FROM notifications_v2 n
            LEFT JOIN threads t ON t.id = n.thread_id
            WHERE n.session_id = ? ORDER BY n.id DESC LIMIT 20
            """,
            (session_id,),
        ).fetchall()
        notifications: list[Notification] = [
            Notification(
                text=f"[{r['user_label']}] {r['message']}"
                if r["user_label"]
                else r["message"],
                kind="info",
                age_secs=_age_secs(r["created_at"]),
            )
            for r in notif_rows
        ]
    except Exception:
        notifications = []

    _dags = _load_graph_dags(conn) if load_graph_dag else []
    graph_dag, graph_dags = (_dags[0], _dags) if _dags else (None, None)

    result = CockpitState(
        topics=topics,
        actions=actions,
        agents=agents,
        notifications=notifications,
        scheduled=fetch_scheduled_tasks(),
        fetched_at=time.time(),
        projects_by_id=projects_by_id if projects_by_id else None,
        graph_by_project=graph_by_project,
        graph_dag=graph_dag,
        graph_dags=graph_dags,
    )
    conn.close()
    return result


def group_threads_by_project(
    topics: list[Topic], projects_by_id: dict[str, str]
) -> list[tuple[str, str, list[Topic]]]:
    """Return [(project_id, project_name, topics)] sorted: named projects first, INBOX last."""
    from collections import defaultdict
    groups: dict[str, list[Topic]] = defaultdict(list)
    for t in topics:
        groups[t.project_id].append(t)
    result = []
    for pid, name in sorted(projects_by_id.items(), key=lambda x: (x[0] == "INBOX", x[0])):
        if pid in groups:
            result.append((pid, name, groups[pid]))
    # topics whose project_id isn't in projects_by_id → INBOX fallback
    known = {pid for pid, _, _ in result}
    leftover = [t for t in topics if t.project_id not in known]
    if leftover and "INBOX" not in known:
        result.append(("INBOX", "Inbox", leftover))
    return result
