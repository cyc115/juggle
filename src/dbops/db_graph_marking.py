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


_ADVANCE_TO_RUNNING = {
    "pending": ("deps_ready", "claim", "dispatch"),
    "ready": ("claim", "dispatch"),
    "dispatching": ("dispatch",),
    "running": (),
}


def mark_exec_failed(db, node_id: str) -> str:
    """Walk the node legally to 'running', then apply 'exec_fail'.

    DA round-2 MAJOR-1 (2026-06-10): agent death (cmd_fail_agent / watchdog
    give-up) never reached the graph — the node stayed 'running' and its
    dependents stalled silently. Also serves the dispatch retry cap
    (dispatching → failed-exec). Raises ValueError on terminal / integrating
    nodes (fail loud, no silent remap).
    """
    from dbops.db_graph import get_node, node_transition

    node = get_node(db, node_id)
    if node is None:
        raise ValueError(f"graph node not found: {node_id!r}")
    state = node["state"]
    if state not in _ADVANCE_TO_RUNNING:
        raise ValueError(
            f"cannot mark exec failure: node {node_id!r} in state {state!r}"
        )
    for event in _ADVANCE_TO_RUNNING[state]:
        node_transition(db, node_id, event)
    return node_transition(db, node_id, "exec_fail")


_BLOCKING_STATES = frozenset(
    {"failed-exec", "failed-integration", "failed-verify", "blocked-failed"}
)


def recompute_blocked(db, project_id: str) -> tuple[list[str], list[str]]:
    """Re-derive blocked-failed from current dep states (after a spec reload).

    DA round-2 BLOCKER-1 (2026-06-10): reloading a fixed spec resurrected the
    failed node but its blocked-failed dependents stayed dead forever (no
    transition out of blocked-failed). Invariant restored here: a node is
    blocked-failed IFF some direct dep is failed-*/blocked-failed. Fixpoint:
      * blocked-failed node with NO blocking dep  → 'reload'   → pending
      * pending node WITH a blocking dep          → 'dep_fail' → blocked-failed
        (covers a blocked node whose content was edited: the load loop reloads
        it to pending while one of its deps is still failed)
    The graph is a DAG and failed-* roots are fixed during the loop, so the
    fixpoint is unique and the loop terminates. Returns (unblocked, reblocked).
    """
    from dbops.db_graph import get_deps, get_node, list_nodes, node_transition

    unblocked: list[str] = []
    reblocked: list[str] = []
    changed = True
    while changed:
        changed = False
        for node in list_nodes(db, project_id):
            if node["state"] not in ("blocked-failed", "pending"):
                continue
            deps = [get_node(db, d) for d in get_deps(db, node["id"])]
            blocking = any(d and d["state"] in _BLOCKING_STATES for d in deps)
            if node["state"] == "blocked-failed" and not blocking:
                node_transition(db, node["id"], "reload")
                unblocked.append(node["id"])
                changed = True
            elif node["state"] == "pending" and blocking:
                node_transition(db, node["id"], "dep_fail")
                reblocked.append(node["id"])
                changed = True
    return unblocked, reblocked


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
