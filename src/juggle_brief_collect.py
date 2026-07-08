"""juggle_brief_collect — live state collector for `juggle brief` (no arg).

``collect_session_state(db)`` composes the EXISTING read paths (threads / agents /
action-items / self-heal / git) into one plain dict — it reimplements none of
those queries. The dict is the deterministic contract: ``--json`` dumps it,
``juggle_brief_render.render_session_human`` formats it.

Agent status is RECONCILED against the live tmux pane (a busy interactive agent
idling at ``❯`` reads as done-but-unreleased, not "busy") via the SAME primitive
the watchdog reaper uses (``juggle_watchdog_stall.is_idle_at_prompt``) — one
definition, never a re-implementation. All live IO is best-effort: a missing
tmux / git / heartbeat degrades a field, never raises.

Topic/project probes live in ``juggle_brief_probe`` (shared helpers imported from
here) to keep each module under the architecture LOC gate.
"""
from __future__ import annotations

import json as _json
from datetime import datetime, timezone

_ARCHIVED_STATES = {"archived"}


def _parse_json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        val = _json.loads(raw)
        return val if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def agent_age(agent: dict, now: datetime | None = None) -> str:
    """Human age from busy_since (if busy) else last_active. Mirrors the
    cmd_list_agents formatter so brief and `agent list` read identically."""
    now = now or datetime.now(timezone.utc)
    ts = agent.get("busy_since") if agent.get("status") == "busy" else None
    ts = ts or agent.get("last_active") or agent.get("last_active_at")
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = int((now - dt).total_seconds())
    except (ValueError, TypeError):
        return "-"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h"


def agent_idle_at_prompt(agent: dict) -> bool:
    """True iff a busy INTERACTIVE agent is parked at its ready prompt (mid-turn
    submission markers ⇒ False). Reuses the watchdog stall primitive; any tmux
    failure ⇒ False (report the DB status unchanged)."""
    try:
        from juggle_agent_interactivity import agent_is_non_interactive

        if agent_is_non_interactive(agent):
            return False
        from juggle_harness import active_pattern_for_agent
        from juggle_tmux import JuggleTmuxManager
        from juggle_watchdog_stall import (
            _capture_pane,
            is_idle_at_prompt,
            markers_for_agent,
        )

        mgr = JuggleTmuxManager()
        content = _capture_pane(mgr, agent.get("pane_id") or "")
        ready, submit = markers_for_agent(agent)
        return is_idle_at_prompt(content, ready, submit, active_pattern_for_agent(agent))
    except Exception:
        return False


def last_pane_line(pane_id: str) -> str:
    """Last non-empty pane line (best-effort; '' on any failure)."""
    try:
        from juggle_tmux import JuggleTmuxManager
        from juggle_watchdog_stall import _capture_pane

        content = _capture_pane(JuggleTmuxManager(), pane_id or "")
        if not content:
            return ""
        for line in reversed(content.splitlines()):
            if line.strip():
                return line.strip()[:120]
    except Exception:
        pass
    return ""


def _watchdog_state() -> dict:
    from juggle_watchdog_health import SINGLETON_PID_FILE, is_watchdog_alive

    pid = None
    try:
        pid = int(SINGLETON_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        pid = None
    return {"alive": is_watchdog_alive(), "pid": pid}


def _autopilot_on() -> bool:
    try:
        from juggle_cmd_autopilot import AUTOPILOT_FLAG

        return AUTOPILOT_FLAG.exists()
    except Exception:
        return False


def _session_id(db) -> str:
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT value FROM session WHERE key='session_id'"
            ).fetchone()
        return row["value"] if row else ""
    except Exception:
        return ""


def _unconsumed_notifs(db, session_id: str) -> int:
    if not session_id:
        return 0
    try:
        watermark = db.get_notif_watermark(session_id) or 0
        return len(db.get_notifications_since_id(session_id, watermark))
    except Exception:
        return 0


def _selfheal_health(db) -> dict:
    try:
        groups = db.get_grouped_error_events(status="open", include_hidden=False)
    except Exception:
        return {"count": 0, "top_sigs": []}
    count = sum(g.get("total_count", 0) for g in groups)
    top = []
    for g in groups[:3]:
        rep = g.get("representative") or {}
        gk = (g.get("group_key") or "")[:8]
        detail = rep.get("exc_type") or rep.get("entrypoint") or "?"
        top.append(f"{gk}: {detail} (x{g.get('total_count', 0)})")
    return {"count": count, "top_sigs": top}


def collect_session_state(db) -> dict:
    """Assemble the no-arg session snapshot dict (active topics only, agents
    reconciled). Reuses db.get_all_threads / get_all_agents / get_open_action_items
    / get_grouped_error_events — no query is reimplemented here."""
    from juggle_context_startup import _get_juggle_version, get_thread_state

    now = datetime.now(timezone.utc)
    current = db.get_current_thread() or ""

    threads = db.get_all_threads()
    active = [
        t
        for t in threads
        if t.get("state") not in _ARCHIVED_STATES and t.get("show_in_list", 1) != 0
    ]

    topics = []
    for t in active:
        tid = t["id"]
        agent = db.get_agent_by_thread(tid)
        topics.append({
            "id": t.get("user_label") or tid[:6],
            "state": t.get("state"),
            "state_badge": get_thread_state(db, t, current),
            "label": t.get("user_label") or t.get("label") or tid[:8],
            "title": t.get("title") or "",
            "open_questions": len(_parse_json_list(t.get("open_questions"))),
            "agent": {"role": agent.get("role"), "status": agent.get("status")}
            if agent
            else None,
        })

    agents = db.get_all_agents()
    busy = [a for a in agents if a.get("status") == "busy"]
    idle = [a for a in agents if a.get("status") == "idle"]
    busy_list = []
    for a in busy:
        topic_lbl = "-"
        if a.get("assigned_thread"):
            th = db.get_thread(a["assigned_thread"])
            if th:
                topic_lbl = th.get("user_label") or th["id"][:6]
        reconciled = "done-unreleased" if agent_idle_at_prompt(a) else "busy"
        busy_list.append({
            "id": a["id"][:8],
            "role": a.get("role") or "-",
            "topic": topic_lbl,
            "age": agent_age(a, now),
            "reconciled": reconciled,
        })

    actions = [
        {"id": it["id"], "priority": it["priority"], "message": it["message"]}
        for it in db.get_open_action_items()
    ]

    session_id = _session_id(db)
    from pathlib import Path

    from juggle_brief_git import git_health

    fallback_repo = str(Path(__file__).resolve().parent.parent)
    health = git_health(active, fallback_repo)
    health["selfheal"] = _selfheal_health(db)

    return {
        "version": _get_juggle_version(),
        "watchdog": _watchdog_state(),
        "autopilot": _autopilot_on(),
        "unconsumed_notifs": _unconsumed_notifs(db, session_id),
        "topics": topics,
        "agents": {"busy": len(busy), "idle": len(idle), "busy_list": busy_list},
        "actions": actions,
        "health": health,
    }
