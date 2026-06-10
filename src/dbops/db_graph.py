"""dbops.db_graph — graph_nodes/graph_edges plan store for project autopilot.

Owns: node/edge CRUD, the node state machine (``node_transition`` is the ONLY
writer of ``graph_nodes.state``), BFS cycle detection, the ready-set query
(all deps ``state='verified'``), and completion marking.
Must not own: dispatching (watchdog, Phase 2), CLI parsing (juggle_cmd_graph),
or any thread-status semantics — the scheduler never reads thread status (DA M5).

Module-level functions take a ``JuggleDB`` handle as their first argument so
they compose with the existing mixin-built DB without widening its surface.
"""

from __future__ import annotations

from dbops.schema import _now

# Node state machine (design 2026-06-10 rev 2):
# pending → ready → dispatching → running → integrating → verified
# failure exits: failed-exec | failed-integration | failed-verify
# dependents of a failed node: blocked-failed (terminal in Phase 1)
VALID_STATES = frozenset(
    {
        "pending",
        "ready",
        "dispatching",
        "running",
        "integrating",
        "verified",
        "failed-exec",
        "failed-integration",
        "failed-verify",
        "blocked-failed",
    }
)

# (current_state, event) -> next_state. Anything else fails loud.
_TRANSITIONS: dict[tuple[str, str], str] = {
    ("pending", "deps_ready"): "ready",
    ("pending", "dep_fail"): "blocked-failed",
    ("pending", "reload"): "pending",
    ("ready", "claim"): "dispatching",
    ("ready", "dep_fail"): "blocked-failed",
    ("ready", "reload"): "pending",
    ("dispatching", "dispatch"): "running",
    ("dispatching", "stale_reset"): "ready",
    ("running", "integrate_start"): "integrating",
    ("running", "exec_fail"): "failed-exec",
    ("integrating", "integrate_ok"): "verified",
    ("integrating", "integrate_fail"): "failed-integration",
    ("integrating", "verify_fail"): "failed-verify",
    # Re-load of an edited spec may resurrect failed nodes (guarded upsert).
    ("failed-exec", "reload"): "pending",
    ("failed-integration", "reload"): "pending",
    ("failed-verify", "reload"): "pending",
}

_EVENTS = frozenset(ev for (_, ev) in _TRANSITIONS)

# Nodes in these states must not be modified by a re-load (guarded upsert).
PROTECTED_STATES = frozenset({"dispatching", "running", "integrating", "verified"})


# ── state machine ──────────────────────────────────────────────────────────────


def node_transition(db, node_id: str, event: str) -> str:
    """Apply ``event`` to the node's state machine. The ONLY state writer.

    Returns the new state. Raises ValueError (fail loud) on an unknown node,
    unknown event, or illegal (state, event) pair — state is left untouched.
    """
    if event not in _EVENTS:
        raise ValueError(f"graph node event unknown: {event!r}")
    node = get_node(db, node_id)
    if node is None:
        raise ValueError(f"graph node not found: {node_id!r}")
    key = (node["state"], event)
    if key not in _TRANSITIONS:
        raise ValueError(
            f"illegal graph transition: node {node_id!r} in state "
            f"{node['state']!r} got event {event!r}"
        )
    new_state = _TRANSITIONS[key]
    now = _now()
    verified_at = now if new_state == "verified" else None
    with db._connect() as conn:
        if verified_at:
            conn.execute(
                "UPDATE graph_nodes SET state=?, verified_at=?, updated_at=? WHERE id=?",
                (new_state, verified_at, now, node_id),
            )
        else:
            conn.execute(
                "UPDATE graph_nodes SET state=?, updated_at=? WHERE id=?",
                (new_state, now, node_id),
            )
        conn.commit()
    return new_state


# ── CRUD (never writes state) ──────────────────────────────────────────────────


def create_node(
    db, *, node_id: str, project_id: str, title: str, prompt: str, verify_cmd=None
) -> None:
    """Insert a new node in state 'pending'. Raises on duplicate id."""
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO graph_nodes (id, project_id, title, prompt, verify_cmd, "
            "state, created_at, updated_at) VALUES (?,?,?,?,?, 'pending', ?, ?)",
            (node_id, project_id, title, prompt, verify_cmd, now, now),
        )
        conn.commit()


def update_node_content(db, node_id: str, *, title: str, prompt: str, verify_cmd) -> None:
    """Update plan content (title/prompt/verify_cmd). Never touches state."""
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_nodes SET title=?, prompt=?, verify_cmd=?, updated_at=? "
            "WHERE id=?",
            (title, prompt, verify_cmd, _now(), node_id),
        )
        conn.commit()


def set_node_thread(db, node_id: str, thread_id) -> None:
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_nodes SET thread_id=?, updated_at=? WHERE id=?",
            (thread_id, _now(), node_id),
        )
        conn.commit()


def set_node_handoff(db, node_id: str, handoff: str) -> None:
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_nodes SET handoff=?, updated_at=? WHERE id=?",
            (handoff, _now(), node_id),
        )
        conn.commit()


def get_node(db, node_id: str) -> dict | None:
    with db._connect() as conn:
        row = conn.execute(
            "SELECT * FROM graph_nodes WHERE id=?", (node_id,)
        ).fetchone()
        return dict(row) if row else None


def get_node_by_thread(db, thread_id: str) -> dict | None:
    with db._connect() as conn:
        row = conn.execute(
            "SELECT * FROM graph_nodes WHERE thread_id=?", (thread_id,)
        ).fetchone()
        return dict(row) if row else None


def list_nodes(db, project_id: str) -> list[dict]:
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM graph_nodes WHERE project_id=? ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def replace_edges(db, node_id: str, dep_ids: list[str]) -> None:
    """Replace the full dependency list of ``node_id``."""
    with db._connect() as conn:
        conn.execute("DELETE FROM graph_edges WHERE node_id=?", (node_id,))
        conn.executemany(
            "INSERT INTO graph_edges (node_id, depends_on_id) VALUES (?,?)",
            [(node_id, dep) for dep in dep_ids],
        )
        conn.commit()


def get_deps(db, node_id: str) -> list[str]:
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT depends_on_id FROM graph_edges WHERE node_id=? ORDER BY depends_on_id",
            (node_id,),
        ).fetchall()
        return [r["depends_on_id"] for r in rows]


def unverified_deps(db, node_id: str) -> list[str]:
    """Dep ids of ``node_id`` whose state is not 'verified' (blocking deps)."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT e.depends_on_id FROM graph_edges e "
            "JOIN graph_nodes d ON d.id = e.depends_on_id "
            "WHERE e.node_id=? AND d.state != 'verified' "
            "ORDER BY e.depends_on_id",
            (node_id,),
        ).fetchall()
        return [r["depends_on_id"] for r in rows]


# ── cycle detection (pure) ─────────────────────────────────────────────────────


def find_cycle(node_ids, edges) -> list[str] | None:
    """Kahn's algorithm over (node_id, depends_on_id) pairs.

    Returns the list of node ids stuck in a cycle, or None for a DAG.
    """
    indegree = {n: 0 for n in node_ids}
    dependents: dict[str, list[str]] = {n: [] for n in node_ids}
    for node, dep in edges:
        indegree[node] += 1
        dependents[dep].append(node)
    queue = [n for n, d in indegree.items() if d == 0]
    seen = 0
    while queue:
        n = queue.pop()
        seen += 1
        for m in dependents[n]:
            indegree[m] -= 1
            if indegree[m] == 0:
                queue.append(m)
    if seen == len(indegree):
        return None
    return sorted(n for n, d in indegree.items() if d > 0)


# ── ready set ──────────────────────────────────────────────────────────────────


def ready_eligible(db, project_id: str) -> list[str]:
    """Pending nodes of ``project_id`` whose deps are ALL 'verified'."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT n.id FROM graph_nodes n WHERE n.project_id=? "
            "AND n.state='pending' AND NOT EXISTS ("
            "  SELECT 1 FROM graph_edges e JOIN graph_nodes d ON d.id=e.depends_on_id"
            "  WHERE e.node_id=n.id AND d.state != 'verified') "
            "ORDER BY n.created_at, n.id",
            (project_id,),
        ).fetchall()
        return [r["id"] for r in rows]


def recompute_ready(db, project_id: str) -> list[str]:
    """Promote every eligible pending node to 'ready'. Returns newly-ready ids."""
    newly = ready_eligible(db, project_id)
    for node_id in newly:
        node_transition(db, node_id, "deps_ready")
    return newly


# ── completion marking ─────────────────────────────────────────────────────────

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
