"""juggle_brief_probe — topic/project state collectors for `juggle brief <ID>`.

``collect_topic_state(db, id)`` resolves a topic slug (e.g. SS) to its
conversation node and gathers state + agent (reconciled) + actions + last-3
notifications + worktree facts, then stamps the computed why-stalled verdict via
``juggle_brief_diagnose.diagnose`` so ``--json`` carries it. ``collect_project_state
(db, id)`` rolls up a project's (P#) task graph. Both REUSE the existing read
paths (threads / agents / action-items / graph-status) and return None when the
id resolves to nothing. Shared IO helpers live in ``juggle_brief_collect``.
"""
from __future__ import annotations

from juggle_brief_collect import (
    _parse_json_list,
    agent_age,
    agent_idle_at_prompt,
    last_pane_line,
)
from dbops.terminal_states import FAILURE_TERMINAL_STATES
from juggle_brief_diagnose import diagnose
from juggle_brief_git import worktree_facts


def collect_topic_state(db, topic_id: str) -> dict | None:
    """Topic probe dict (or None if ``topic_id`` resolves to no conversation)."""
    thread = db.get_thread_by_user_label(topic_id)
    if thread is None:
        return None
    tid = thread["id"]
    state = thread.get("state")

    agent_row = db.get_agent_by_thread(tid)
    agent = None
    if agent_row:
        agent = {
            "id": agent_row["id"][:8],
            "role": agent_row.get("role"),
            "status": agent_row.get("status"),
            "age": agent_age(agent_row),
            "last_line": last_pane_line(agent_row.get("pane_id") or ""),
            "idle_at_prompt": agent_idle_at_prompt(agent_row),
        }

    actions = [
        {"id": it["id"], "priority": it["priority"], "message": it["message"],
         "type": it.get("type")}
        for it in db.get_open_action_items()
        if it.get("thread_id") == tid
    ]
    has_failure_action = any(it.get("type") == "failure" for it in actions)

    notifications = _topic_notifications(db, tid)
    worktree = worktree_facts(thread)
    work_landed = _topic_work_landed(db, tid)

    topic_state = {
        "id": thread.get("user_label") or tid[:6],
        "thread_id": tid,
        "title": thread.get("title") or "",
        "summary": thread.get("summary") or "",
        "state": state,
        "open_questions": _parse_json_list(thread.get("open_questions")),
        "agent": agent,
        "actions": actions,
        "has_failure_action": has_failure_action,
        "notifications": notifications,
        "worktree": worktree,
        "work_landed": work_landed,
        "unmerged_commits": worktree.get("unmerged_commits", 0),
        "deps_unmet": False,
    }
    topic_state["verdict"] = diagnose(topic_state)
    return topic_state


def _topic_notifications(db, tid: str) -> list[dict]:
    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT message, created_at FROM notifications_v2 "
                "WHERE thread_id = ? ORDER BY id DESC LIMIT 3",
                (tid,),
            ).fetchall()
        return [{"message": r["message"], "created_at": r["created_at"]} for r in rows]
    except Exception:
        return []


def _topic_work_landed(db, tid: str) -> bool:
    try:
        from dbops.db_topics import get_topic_by_thread
        from dbops.terminal_states import topic_work_landed

        return topic_work_landed(get_topic_by_thread(db, tid))
    except Exception:
        return False


def collect_project_state(db, project_id: str) -> dict | None:
    """Project probe dict (or None if ``project_id`` is not a project)."""
    project = db.get_project(project_id)
    if project is None:
        return None

    from juggle_autopilot_state import get_armed_projects
    from juggle_graph_status import _titled, graph_counts

    counts = graph_counts(db, project_id) or {}
    try:
        armed = project_id in get_armed_projects(db)
    except Exception:
        armed = False

    next_node = None
    for states in (("ready",), ("open",)):
        titled = _titled(db, project_id, states)
        if titled:
            next_node = {"id": titled[0][0], "title": titled[0][1]}
            break

    failing_nodes = [
        nid for nid, _ in _titled(db, project_id, tuple(FAILURE_TERMINAL_STATES))
    ]

    return {
        "id": project_id,
        "name": project.get("name") or "",
        "armed": armed,
        "counts": counts,
        "next_node": next_node,
        "failing_nodes": failing_nodes,
    }
