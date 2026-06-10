"""
juggle_cmd_agents_graph — graph-node glue for the agent CLI (autopilot).

Owns: mark_graph_node (maps a thread completion's integrate outcome onto the
bound graph node + ready-set notifications/action items; notify ONLY —
dispatch is watchdog-owned, DA B4/M1), enforce_handoff_contract (DA M4:
complete-agent refuses nodes-with-dependents without --handoff), and
check_node_guard (DA B5: send-task refuses tick-owned nodes sans --force-node).
Must not own: completion/failure handlers (juggle_cmd_agents_complete) or
node state semantics (dbops.db_graph).
"""

from __future__ import annotations

import sys

# Node states where a completion is still meaningful (mirrors db_graph
# mark_completion's legal walk); terminal/blocked nodes skip enforcement —
# a double-completion stays the Phase 1 warn+no-op, never a refusal.
_ENFORCEABLE_STATES = frozenset(
    {"pending", "ready", "dispatching", "running", "integrating"}
)


def _node_for_thread(db, thread_uuid):
    """Bound node for a thread, or None (incl. pre-migration DBs)."""
    from dbops import db_graph

    try:
        return db_graph.get_node_by_thread(db, thread_uuid)
    except Exception:
        return None


def enforce_handoff_contract(db, thread_uuid, handoff) -> None:
    """DA M4: a graph node with dependents MUST hand off. Exits 1 on violation.

    Runs BEFORE any completion side effects — dependent prompts are hydrated
    from this handoff, so an empty one is garbage-in for every downstream node.
    """
    from dbops import db_graph

    node = _node_for_thread(db, thread_uuid)
    if not node or node["state"] not in _ENFORCEABLE_STATES:
        return
    if handoff and str(handoff).strip():
        return
    dependents = db_graph.get_dependents(db, node["id"])
    if not dependents:
        return
    print(
        f"Error: graph node {node['id']} has dependents ({', '.join(dependents)}) "
        f"which are hydrated from its handoff — re-run with "
        f"--handoff '<files touched, interfaces added/changed, key decisions, "
        f"follow-ups>'. Nothing was marked or closed."
    )
    sys.exit(1)


def check_node_guard(db, thread_uuid, *, force: bool) -> str | None:
    """DA B5: manual send-task to a tick-owned node is a double-dispatch race.

    Returns a refusal message, or None when dispatch may proceed (unbound
    thread, operator-territory node state, or --force-node).
    """
    from dbops import db_graph

    if force or not thread_uuid:
        return None
    node = _node_for_thread(db, thread_uuid)
    if not node or node["state"] not in db_graph.TICK_OWNED_STATES:
        return None
    return (
        f"thread is bound to graph node {node['id']} in tick-owned state "
        f"{node['state']!r} — the autopilot watchdog tick dispatches it. "
        f"Use --force-node to override (bypasses the single-dispatcher claim)."
    )


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
