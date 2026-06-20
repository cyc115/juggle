"""juggle_add_node — unified node creation verb (P5).

Backs `juggle add-node` CLI. create-thread and graph add-task shim into here.

DUAL-WRITE CONTRACT (valid through P8):
  kind=task        → nodes + graph_tasks + graph_edges + node_edges
  kind=conversation → nodes + threads (via db.create_thread)
  kind=research    → nodes only (new kind; no legacy table)
  kind=decision    → nodes only (new kind; no legacy table)

P8 removes the legacy writes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from dbops.db_node_machine import node_transition, _KIND_LEGAL  # noqa: F401


VALID_KINDS = frozenset({"task", "research", "conversation", "decision"})

INBOX_PROJECT_ID = "INBOX"


def ensure_inbox_project(db) -> None:
    """Create the INBOX project row if it doesn't already exist (idempotent)."""
    if db.get_project(INBOX_PROJECT_ID) is not None:
        return
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO projects "
            "(id, name, objective, success_criteria, out_of_scope, status, created_at, last_active) "
            "VALUES (?, 'INBOX', 'Default inbox for untagged nodes', '[]', '', 'active', ?, ?)",
            (INBOX_PROJECT_ID, now, now),
        )
        conn.commit()


class AddNodeError(ValueError):
    """Validation or guard failure for add_node. Nothing written on raise."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _all_node_ids(conn) -> frozenset:
    """All ids currently in the nodes table."""
    return frozenset(
        r[0] for r in conn.execute("SELECT id FROM nodes").fetchall()
    )


def _all_legacy_task_ids(conn) -> frozenset:
    """All ids in graph_tasks — needed during transition for dep validation."""
    try:
        return frozenset(
            r[0] for r in conn.execute("SELECT id FROM graph_tasks").fetchall()
        )
    except Exception:
        return frozenset()


def _node_edges_for(conn, project_id) -> list[tuple[str, str]]:
    """All (node_id, depends_on_id) edges currently in node_edges."""
    return [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT node_id, depends_on_id FROM node_edges"
        ).fetchall()
    ]



def add_node(
    db,
    *,
    kind: str,
    title: str,
    objective: str = "",
    project_id: str | None = None,
    deps: list[str] | None = None,
    required_by: list[str] | None = None,
    verify_cmd: str | None = None,
    parent_id: str | None = None,
    node_id: str | None = None,
) -> dict:
    """Create a new unified node. Returns {'node_id': str, 'state': str}.

    Validates, then inserts into nodes (and dual-writes to legacy tables).
    Raises AddNodeError on any validation failure — nothing written.
    """
    deps = list(deps or [])
    required_by = list(required_by or [])

    if kind not in VALID_KINDS:
        raise AddNodeError(f"unknown kind {kind!r}; valid: {sorted(VALID_KINDS)}")

    if verify_cmd and kind != "task":
        raise AddNodeError(
            f"--verify-cmd is only allowed for kind=task, not kind={kind!r}"
        )

    if kind == "conversation":
        return _add_conversation(db, title=title, project_id=project_id)

    if kind == "decision":
        return _add_node_only(
            db, kind="decision", title=title, objective=objective,
            project_id=project_id,
        )

    if kind == "research":
        return _add_node_only(
            db, kind="research", title=title, objective=objective,
            project_id=project_id,
        )

    # kind == "task"
    return _add_task_node(
        db,
        title=title,
        objective=objective,
        project_id=project_id,
        deps=deps,
        required_by=required_by,
        verify_cmd=verify_cmd,
        parent_id=parent_id,
        node_id=node_id,
    )


def _add_conversation(db, *, title: str, project_id: str | None) -> dict:
    """Create a conversation node. Dual-writes to threads via db.create_thread."""
    thread_id = db.create_thread(title, session_id="", project_id=project_id)
    _create_node_row(
        db, node_id=thread_id, kind="conversation", title=title,
        objective="", state="open", project_id=project_id,
        verify_cmd=None, parent_id=None,
    )
    return {"node_id": thread_id, "state": "open"}


def _add_node_only(
    db, *, kind: str, title: str, objective: str, project_id: str | None,
) -> dict:
    """Create a node with no legacy dual-write (research/decision)."""
    node_id = str(uuid.uuid4())
    _create_node_row(
        db, node_id=node_id, kind=kind, title=title, objective=objective,
        state="open", project_id=project_id, verify_cmd=None, parent_id=None,
    )
    return {"node_id": node_id, "state": "open"}


def _add_task_node(
    db,
    *,
    title: str,
    objective: str,
    project_id: str | None,
    deps: list[str],
    required_by: list[str],
    verify_cmd: str | None,
    parent_id: str | None,
    node_id: str | None = None,
) -> dict:
    """Create a task node with dual-write to graph_tasks + edges."""
    from juggle_graph_upsert import find_cycle, lint_verify_cmd

    if verify_cmd:
        err = lint_verify_cmd(verify_cmd)
        if err:
            raise AddNodeError(f"verify_cmd invalid: {err}")

    node_id = node_id or str(uuid.uuid4())
    now = _now()

    conn = db._connect()
    try:
        existing_nodes = _all_node_ids(conn)
        legacy_tasks = _all_legacy_task_ids(conn)
        all_valid = existing_nodes | legacy_tasks

        for dep in deps:
            if dep == node_id:
                raise AddNodeError(f"node cannot depend on itself")
            if dep not in all_valid:
                raise AddNodeError(f"unknown dep {dep!r}")

        for rb in required_by:
            if rb == node_id:
                raise AddNodeError(f"node cannot be required_by itself")
            if rb not in all_valid:
                raise AddNodeError(f"unknown required_by target {rb!r}")

        # Cycle check over node_edges + new edges
        all_node_ids_list = sorted(existing_nodes | {node_id})
        existing_edges = _node_edges_for(conn, project_id)
        new_edges = [(node_id, d) for d in deps] + [(rb, node_id) for rb in required_by]
        cycle = find_cycle(all_node_ids_list, existing_edges + new_edges)
        if cycle:
            raise AddNodeError(f"dependency cycle would form involving: {', '.join(cycle)}")

        # Insert into nodes (state='open' initially)
        conn.execute(
            """INSERT INTO nodes
               (id, kind, title, objective, state, project_id, parent_id,
                verify_cmd, created_at, updated_at)
               VALUES (?, 'task', ?, ?, 'open', ?, ?, ?, ?, ?)""",
            (node_id, title, objective, project_id, parent_id, verify_cmd, now, now),
        )

        # Dual-write: graph_tasks (state='pending' to match legacy convention)
        conn.execute(
            """INSERT INTO graph_tasks
               (id, project_id, title, prompt, verify_cmd, state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (node_id, project_id or "INBOX", title, objective, verify_cmd, now, now),
        )

        # Write node_edges
        for dep in deps:
            conn.execute(
                "INSERT OR IGNORE INTO node_edges (node_id, depends_on_id) VALUES (?,?)",
                (node_id, dep),
            )
        for rb in required_by:
            conn.execute(
                "INSERT OR IGNORE INTO node_edges (node_id, depends_on_id) VALUES (?,?)",
                (rb, node_id),
            )

        # Dual-write: graph_edges (for legacy recompute_ready)
        for dep in deps:
            conn.execute(
                "INSERT OR IGNORE INTO graph_edges (task_id, depends_on_id) VALUES (?,?)",
                (node_id, dep),
            )
        for rb in required_by:
            conn.execute(
                "INSERT OR IGNORE INTO graph_edges (task_id, depends_on_id) VALUES (?,?)",
                (rb, node_id),
            )

        conn.commit()
    except AddNodeError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # After commit: determine readiness.
    # Use legacy recompute_ready to promote graph_tasks (it also handles
    # required_by demotion). Then sync nodes.state to match.
    effective_project = project_id or "INBOX"
    from dbops import db_graph
    db_graph.recompute_ready(db, effective_project)

    task = db_graph.get_task(db, node_id)
    graph_state = task["state"] if task else "pending"

    if graph_state == "ready":
        # Promote the nodes row
        _update_node_state(db, node_id, "ready", now=_now())
        _poke(db)
        return {"node_id": node_id, "state": "ready"}

    return {"node_id": node_id, "state": "open"}


def _update_node_state(db, node_id: str, state: str, now: str) -> None:
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET state=?, updated_at=? WHERE id=?",
            (state, now, node_id),
        )
        conn.commit()


def _poke(db) -> None:
    """Signal the watchdog to tick immediately (no-op if not running)."""
    try:
        from juggle_watchdog_poke import poke_watchdog
        poke_watchdog(db.db_path)
    except Exception:
        pass


def _create_node_row(
    db,
    *,
    node_id: str,
    kind: str,
    title: str,
    objective: str,
    state: str,
    project_id: str | None,
    verify_cmd: str | None,
    parent_id: str | None,
) -> None:
    """Insert a node row. Used by shims that already wrote to the legacy table."""
    now = _now()
    with db._connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO nodes
               (id, kind, title, objective, state, project_id, parent_id,
                verify_cmd, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, kind, title, objective, state, project_id,
             parent_id, verify_cmd, now, now),
        )
        conn.commit()
