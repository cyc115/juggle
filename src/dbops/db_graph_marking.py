"""dbops.db_graph_marking — completion marking for the autopilot plan store.

Owns: mapping a thread completion's (integrate outcome, verify outcome) onto
the node state machine (``mark_completion``).
Must not own: the state machine itself or node CRUD (dbops.db_graph — every
state write here goes through its ``node_transition``), dispatching
(juggle_graph_dispatch), or notifications/action items
(juggle_cmd_agents_graph).

``dbops.db_graph`` re-exports ``mark_completion`` so callers keep a single
graph-store import seam.
"""

from __future__ import annotations

_ADVANCE_TO_INTEGRATING = {
    "pending": ("deps_ready", "claim", "dispatch", "integrate_start"),
    "ready": ("claim", "dispatch", "integrate_start"),
    "dispatching": ("dispatch", "integrate_start"),
    "running": ("integrate_start",),
    "integrating": (),
}


def mark_completion(
    db, node_id: str, *, integrate_ok: bool, verify_ok: bool = True, handoff=None
) -> str:
    """Map a thread completion onto the node state machine. Returns final state.

    Walks the node legally to 'integrating', then applies the outcome:
    integrate failure → 'failed-integration' (DA B3: NEVER 'verified'),
    verify failure → 'failed-verify', else → 'verified'. Raises ValueError
    if the node is in a terminal/blocked state (fail loud, no silent remap).
    """
    # Deferred import: db_graph re-exports this module's functions at its
    # bottom, so a module-level import here would be import-order sensitive.
    from dbops.db_graph import get_node, node_transition, set_node_handoff

    node = get_node(db, node_id)
    if node is None:
        raise ValueError(f"graph node not found: {node_id!r}")
    state = node["state"]
    if state not in _ADVANCE_TO_INTEGRATING:
        raise ValueError(
            f"cannot mark completion: node {node_id!r} in terminal state {state!r}"
        )
    if handoff is not None:
        set_node_handoff(db, node_id, handoff)
    for event in _ADVANCE_TO_INTEGRATING[state]:
        node_transition(db, node_id, event)
    if not integrate_ok:
        return node_transition(db, node_id, "integrate_fail")
    if not verify_ok:
        return node_transition(db, node_id, "verify_fail")
    return node_transition(db, node_id, "integrate_ok")
