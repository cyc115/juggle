"""dbops.db_graph_marking — completion marking + failure propagation.

Owns: mapping a thread completion's (integrate outcome, verify outcome) onto
the node state machine (``mark_completion``) and blocking the transitive
dependents of a failed node (``propagate_failure`` — design rev 2: no silent
stall, dependents go 'blocked-failed').
Must not own: the state machine itself or node CRUD (dbops.db_graph — every
state write here goes through its ``node_transition``), dispatching
(juggle_graph_dispatch), or notifications/action items
(juggle_cmd_agents_graph).

``dbops.db_graph`` re-exports these so callers keep a single graph-store
import seam.
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


def propagate_failure(db, node_id: str) -> list[str]:
    """Block ALL transitive dependents of a failed node. Returns blocked ids.

    Design rev 2 / Phase 3 (2026-06-10): a node in failed-exec |
    failed-integration | failed-verify must not leave its downstream silently
    'pending' forever. BFS over the dependents closure; every node still in
    'pending' or 'ready' transitions via 'dep_fail' → 'blocked-failed'
    (through the sole state writer). Idempotent: already-blocked/terminal
    dependents are skipped, so diamond shapes block each node exactly once
    and re-propagation returns []. Siblings (non-dependents) are untouched.
    """
    from dbops.db_graph import get_dependents, get_node, node_transition

    blocked: list[str] = []
    seen = {node_id}
    queue = [node_id]
    while queue:
        for dep_id in get_dependents(db, queue.pop(0)):
            if dep_id in seen:
                continue
            seen.add(dep_id)
            node = get_node(db, dep_id)
            if node and node["state"] in ("pending", "ready"):
                node_transition(db, dep_id, "dep_fail")
                blocked.append(dep_id)
            queue.append(dep_id)
    return blocked
