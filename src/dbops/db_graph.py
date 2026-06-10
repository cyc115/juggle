"""dbops.db_graph — graph_nodes/graph_edges plan store for project autopilot.

Owns: node/edge CRUD, the node state machine (``node_transition`` is the ONLY
writer of ``graph_nodes.state``), the ready-set query (all deps
``state='verified'``), and completion marking.
Must not own: dispatching (juggle_graph_dispatch — whose atomic ready→
dispatching claim is the one sanctioned writer besides ``node_transition``),
CLI parsing / spec validation (juggle_cmd_graph), or any thread-status
semantics — the scheduler never reads thread status (DA M5).

Module-level functions take a ``JuggleDB`` handle as their first argument so
they compose with the existing mixin-built DB without widening its surface.
"""

from __future__ import annotations

from contextlib import contextmanager

from dbops.schema import _now


@contextmanager
def _cx(db, conn=None):
    """Yield a write connection. When the caller passes ``conn`` it owns the
    transaction (no commit here) — multi-node spec loads are all-or-nothing
    (DA round-2 BLOCKER-1c, 2026-06-10). Otherwise open, commit, close."""
    if conn is not None:
        yield conn
        return
    c = db._connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()

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
    # DA round-2 BLOCKER-1 (2026-06-10): blocked-failed was a dead end — the
    # blocked tail of a failed node could never resume after a spec reload.
    ("blocked-failed", "reload"): "pending",
}

_EVENTS = frozenset(ev for (_, ev) in _TRANSITIONS)

# Nodes in these states must not be modified by a re-load (guarded upsert).
PROTECTED_STATES = frozenset({"dispatching", "running", "integrating", "verified"})

# Tick-owned states (DA B5): a thread bound to a node in one of these is
# dispatched by the watchdog tick — manual send-task must refuse without
# --force-node. pending/failed-*/blocked-failed remain operator territory.
TICK_OWNED_STATES = frozenset(
    {"ready", "dispatching", "running", "integrating", "verified"}
)


# ── state machine ──────────────────────────────────────────────────────────────


def node_transition(db, node_id: str, event: str, conn=None) -> str:
    """Apply ``event`` to the node's state machine. The ONLY state writer.

    Returns the new state. Raises ValueError (fail loud) on an unknown node,
    unknown event, or illegal (state, event) pair — state is left untouched.
    """
    if event not in _EVENTS:
        raise ValueError(f"graph node event unknown: {event!r}")
    node = get_node(db, node_id, conn=conn)
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
    sets, params = ["state=?", "updated_at=?"], [new_state, now]
    if new_state == "verified":
        sets.append("verified_at=?")
        params.append(now)
    if event == "reload":
        # A resurrected node must not keep its dead thread's id (DA round-2
        # minor 4, 2026-06-10): stale bindings resolved to closed threads.
        sets.append("thread_id=NULL")
    with _cx(db, conn) as c:
        c.execute(
            f"UPDATE graph_nodes SET {', '.join(sets)} WHERE id=?",
            (*params, node_id),
        )
    return new_state


# ── CRUD (never writes state) ──────────────────────────────────────────────────


def create_node(
    db, *, node_id: str, project_id: str, title: str, prompt: str, verify_cmd=None,
    conn=None,
) -> None:
    """Insert a new node in state 'pending'. Raises on duplicate id."""
    now = _now()
    with _cx(db, conn) as c:
        c.execute(
            "INSERT INTO graph_nodes (id, project_id, title, prompt, verify_cmd, "
            "state, created_at, updated_at) VALUES (?,?,?,?,?, 'pending', ?, ?)",
            (node_id, project_id, title, prompt, verify_cmd, now, now),
        )


def update_node_content(
    db, node_id: str, *, title: str, prompt: str, verify_cmd, conn=None
) -> None:
    """Update plan content (title/prompt/verify_cmd). Never touches state."""
    with _cx(db, conn) as c:
        c.execute(
            "UPDATE graph_nodes SET title=?, prompt=?, verify_cmd=?, updated_at=? "
            "WHERE id=?",
            (title, prompt, verify_cmd, _now(), node_id),
        )


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


def set_node_diffstat(db, node_id: str, diffstat: str) -> None:
    """Pre-merge diffstat captured by integrate (hydration enrichment)."""
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_nodes SET diffstat=?, updated_at=? WHERE id=?",
            (diffstat, _now(), node_id),
        )
        conn.commit()


def get_node(db, node_id: str, conn=None) -> dict | None:
    with _cx(db, conn) as c:
        row = c.execute(
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


def replace_edges(db, node_id: str, dep_ids: list[str], conn=None) -> None:
    """Replace the full dependency list of ``node_id``."""
    with _cx(db, conn) as c:
        c.execute("DELETE FROM graph_edges WHERE node_id=?", (node_id,))
        c.executemany(
            "INSERT INTO graph_edges (node_id, depends_on_id) VALUES (?,?)",
            [(node_id, dep) for dep in dep_ids],
        )


def get_deps(db, node_id: str) -> list[str]:
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT depends_on_id FROM graph_edges WHERE node_id=? ORDER BY depends_on_id",
            (node_id,),
        ).fetchall()
        return [r["depends_on_id"] for r in rows]


def get_dependents(db, node_id: str) -> list[str]:
    """Node ids that depend on ``node_id`` (reverse edges)."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT node_id FROM graph_edges WHERE depends_on_id=? ORDER BY node_id",
            (node_id,),
        ).fetchall()
        return [r["node_id"] for r in rows]


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


# ── completion marking + failure propagation (extracted seam) ─────────────────

from dbops.db_graph_marking import (  # noqa: E402,F401
    mark_completion,
    mark_exec_failed,
    propagate_failure,
    recompute_blocked,
)
