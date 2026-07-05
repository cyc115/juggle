"""juggle_graph_dispatch_resolve — per-node dispatch-attribute resolvers.

Extracted from juggle_graph_dispatch (2026-07-05, loop-entity V2/P2 LOC gate:
the dispatcher module was at its 355-line allowlist budget and P2's per-node
model resolver needed headroom). Resolves the role a graph node/topic dispatches
with. juggle_graph_dispatch re-exports this (bottom import) so callers/tests keep
importing it from there unchanged.

TASK_ROLE is imported lazily (call-time) to avoid a top-level import cycle with
juggle_graph_dispatch (which re-exports this module at its own bottom).
"""
from __future__ import annotations


def _resolve_dispatch_role(db, node: dict | None) -> str:
    """The role a node/topic dispatches as (loop-entity Phase 2).

    Reads nodes.role by id (Phase-1 column, DEFAULT 'coder' → legacy graphs
    unchanged), preferring any 'role' the caller already hydrated onto the dict,
    then falling back to 'coder'. coder/planner get an isolated worktree (the
    send-path gate keys off the acquired agent's role); researcher runs
    read-only in place (no worktree)."""
    from juggle_graph_dispatch import TASK_ROLE

    node = node or {}
    role = node.get("role")
    nid = node.get("id")
    if not role and nid:
        try:
            with db._connect() as conn:
                row = conn.execute(
                    "SELECT role FROM nodes WHERE id=?", (nid,)
                ).fetchone()
            role = (row["role"] if not isinstance(row, tuple) else row[0]) if row else None
        except Exception:
            role = None
    return role or TASK_ROLE


def _resolve_dispatch_model(db, node: dict | None):
    """The model a node/topic dispatches with (loop-entity V2 / P2), best-effort.

    Prefer a ``model`` the caller already hydrated onto the dict (topic rows carry
    it, _TOPIC_SELECT). Else read nodes.model by id at TOPIC grain; a kind='task'
    node with no own model inherits its parent topic's model via ``parent_id``
    (a topic = one pane = one model). NULL → None (harness default / role-resolved
    — today's behavior). Fail-soft: a pre-migration DB (no model column) resolves
    None, so the model is simply not threaded."""
    node = node or {}
    model = node.get("model")
    if model:
        return model
    nid = node.get("id")
    if not nid:
        return None
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT model, kind, parent_id FROM nodes WHERE id=?", (nid,)
            ).fetchone()
            if row is None:
                return None
            if row["model"]:
                return row["model"]
            if row["kind"] == "task" and row["parent_id"]:
                prow = conn.execute(
                    "SELECT model FROM nodes WHERE id=?", (row["parent_id"],)
                ).fetchone()
                return prow["model"] if prow else None
    except Exception:
        return None
    return None
