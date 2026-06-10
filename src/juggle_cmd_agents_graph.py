"""
juggle_cmd_agents_graph — graph-node marking for agent completion (autopilot Phase 1).

Owns: mark_graph_node — maps a thread completion's integrate outcome onto the
bound graph node via dbops.db_graph and emits ready-set notifications/action
items. Notify ONLY; dispatch is watchdog-owned (Phase 2, DA B4/M1).
Must not own: completion/failure handlers (juggle_cmd_agents_complete) or
node state semantics (dbops.db_graph).
"""

from __future__ import annotations


def mark_graph_node(db, thread_uuid, integrate_ok, handoff, session_id):
    """If the thread is bound to a graph node, record the completion outcome.

    Maps (integrate outcome) → node event via dbops.db_graph.mark_completion:
    success → 'verified' (stored verified_at), failure → 'failed-integration'
    (DA B3: never 'verified'). Recomputes the ready set and emits a
    notification + action item per newly-ready node. NEVER dispatches.
    """
    from dbops import db_graph

    try:
        node = db_graph.get_node_by_thread(db, thread_uuid)
    except Exception:
        return  # pre-migration DB without graph tables — nothing to mark
    if not node:
        return

    try:
        state = db_graph.mark_completion(
            db, node["id"], integrate_ok=integrate_ok, handoff=handoff
        )
    except ValueError as e:
        print(f"Warning: graph node {node['id']} not marked — {e}")
        return

    if state == "verified":
        db.add_notification_v2(
            thread_id=thread_uuid,
            message=f"⬢ graph node {node['id']} verified",
            session_id=session_id,
        )
    else:
        db.add_notification_v2(
            thread_id=thread_uuid,
            message=f"⬢ graph node {node['id']} → {state}",
            session_id=session_id,
        )
        db.add_action_item(
            thread_id=thread_uuid,
            message=f"⚠️ Graph node {node['id']} ended in {state} — fix before dependents can run.",
            type_="failure",
            priority="high",
        )

    for ready_id in db_graph.recompute_ready(db, node["project_id"]):
        ready_node = db_graph.get_node(db, ready_id)
        title = ready_node["title"] if ready_node else ready_id
        db.add_notification_v2(
            thread_id=None,
            message=f"⬢ graph node ready: {ready_id} — {title}",
            session_id=session_id,
        )
        db.add_action_item(
            thread_id=None,
            message=f"Graph node ready to dispatch: {ready_id} — {title}",
            type_="manual_step",
            priority="normal",
        )
