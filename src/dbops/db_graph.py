"""dbops.db_graph — graph_tasks/graph_edges plan store for project autopilot.

Owns: task/edge CRUD, the task state machine (``task_transition`` is the ONLY
writer of ``graph_tasks.state``), the ready-set query (all deps
``state='verified'``), and completion marking.
Must not own: dispatching (juggle_graph_dispatch — whose atomic ready→
dispatching claim is the one sanctioned writer besides ``task_transition``),
CLI parsing / spec validation (juggle_cmd_graph), or any thread-status
semantics — the scheduler never reads thread status (DA M5).

Module-level functions take a ``JuggleDB`` handle as their first argument so
they compose with the existing mixin-built DB without widening its surface.
"""

from __future__ import annotations

from contextlib import contextmanager

from dbops.schema import _now
from dbops.db_node_machine import InvalidTransition, legal_events, node_transition
from dbops.dispatch_edge import bind_dispatch_thread
from dbops.state_write import cas_state, write_state


@contextmanager
def _cx(db, conn=None):
    """Yield a write connection. A caller-passed ``conn`` owns the transaction
    (no commit — all-or-nothing loads, BLOCKER-1c); else open, commit, close."""
    if conn is not None:
        yield conn
        return
    c = db._connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()

# Task state machine (design 2026-06-10 rev 2; vocab unified to 'open' in P8):
# open → ready → dispatching → running → integrating → verified
# failure exits: failed-exec | failed-integration | failed-verify
# dependents of a failed task: blocked-failed (terminal in Phase 1)
# The (state, event) transition DECISION is delegated to the unified
# db_node_machine.node_transition (kind='task') — db_graph owns no second table.
VALID_STATES = frozenset(
    {
        "open", "ready", "dispatching", "running", "integrating",
        "verified", "failed-exec", "failed-integration", "failed-verify",
        "blocked-failed",
    }
)

# Tasks in these states must not be modified by a re-load (guarded upsert).
PROTECTED_STATES = frozenset({"dispatching", "running", "integrating", "verified"})

# Tick-owned states (DA B5): a thread bound to a task in one of these is
# dispatched by the watchdog tick — manual send-task must refuse without
# --force-task. pending/failed-*/blocked-failed remain operator territory.
TICK_OWNED_STATES = frozenset(
    {"ready", "dispatching", "running", "integrating", "verified"}
)

# Topic/task discriminator (P8 M2): topics are now their OWN kind='topic' nodes
# (Migration 53), so the task set is simply kind='task' — bare tasks and a topic's
# child tasks. The legacy graph_topics-membership anti-join is retired; topics can
# never leak into the task set because they no longer carry kind='task'.

# nodes projection reproducing the legacy graph_tasks row shape so task-execution
# consumers keep their column names after the P8 read-flip (Task 4.1): objective→
# prompt, parent_id→topic_id, thread_id from the kind='dispatch' node_edge (M1/Q2).
_TASK_SELECT = (
    "SELECT id, project_id, title, objective AS prompt, verify_cmd, state, "
    "(SELECT depends_on_id FROM node_edges WHERE node_id=nodes.id AND kind='dispatch' LIMIT 1) AS thread_id, "
    "parent_id AS topic_id, handoff, diffstat, verified_at, "
    "verify_retries, verify_failure, priority, created_at, updated_at "
    "FROM nodes WHERE kind='task'"
)


# ── state machine ──────────────────────────────────────────────────────────────


def task_transition(db, task_id: str, event: str, conn=None) -> str:
    """Apply ``event`` to the task's state machine. The ONLY state writer.

    The transition DECISION is delegated to the unified
    ``db_node_machine.node_transition`` (kind='task') — db_graph owns no second
    transition table. Returns the new state. Raises ValueError (fail loud) on an
    unknown task, unknown event, or illegal (state, event) pair — state is left
    untouched.
    """
    if event not in legal_events("task"):
        raise ValueError(f"graph task event unknown: {event!r}")
    task = get_task(db, task_id, conn=conn)
    if task is None:
        raise ValueError(f"graph task not found: {task_id!r}")
    try:
        new_state = node_transition(task["state"], event, "task")
    except InvalidTransition as e:
        raise ValueError(
            f"illegal graph transition: task {task_id!r} in state "
            f"{task['state']!r} got event {event!r}"
        ) from e
    # Single lockstep writer: nodes + graph_tasks together (M3). 'reload' clears
    # the dead thread binding (DA round-2 minor 4, 2026-06-10: stale bindings
    # resolved to closed threads).
    with _cx(db, conn) as c:
        write_state(c, task_id, new_state, now=_now(),
                    verified=(new_state == "verified"),
                    clear_thread=(event == "reload"))
    return new_state


# ── CRUD (never writes state) ──────────────────────────────────────────────────


def create_task(
    db, *, task_id: str, project_id: str, title: str, prompt: str, verify_cmd=None,
    priority: int = 0, conn=None,
) -> None:
    """Insert a new task node in state 'open' (raises on dup). Writes ONLY the
    authoritative nodes row (objective=prompt) — the legacy graph_tasks INSERT was
    cut (P8 c4-write-cut); nodes is the sole task store. ``priority`` (default 0)
    lifts fix/defect tasks ahead in the ready-dispatch order."""
    now = _now()
    with _cx(db, conn) as c:
        c.execute(
            "INSERT INTO nodes (id, kind, title, objective, state, project_id, "
            "verify_cmd, priority, created_at, updated_at) "
            "VALUES (?, 'task', ?, ?, 'open', ?, ?, ?, ?, ?)",
            (task_id, title, prompt, project_id, verify_cmd, priority, now, now),
        )


def update_task_content(
    db, task_id: str, *, title: str, prompt: str, verify_cmd, conn=None
) -> None:
    """Update plan content (title/prompt/verify_cmd). Never touches state.

    Writes ONLY nodes (objective=prompt) — the legacy graph_tasks UPDATE was cut
    (P8 c4-write-cut).
    """
    now = _now()
    with _cx(db, conn) as c:
        c.execute(
            "UPDATE nodes SET title=?, objective=?, verify_cmd=?, updated_at=? "
            "WHERE id=? AND kind='task'",
            (title, prompt, verify_cmd, now, task_id),
        )


def set_task_thread(db, task_id: str, thread_id) -> None:
    """Bind the dispatch thread as a typed kind='dispatch' node_edge (P8 M1/Q2;
    thread_id=None unbinds). The legacy graph_tasks.thread_id write was cut
    (P8 c4-write-cut)."""
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET updated_at=? WHERE id=? AND kind='task'",
            (now, task_id),
        )
        bind_dispatch_thread(conn, task_id, thread_id)
        conn.commit()


def set_task_handoff(db, task_id: str, handoff: str) -> None:
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET handoff=?, updated_at=? WHERE id=? AND kind='task'",
            (handoff, now, task_id),
        )
        conn.commit()


def bump_verify_retry(db, task_id: str, failure: str | None) -> None:
    """Verify-fallback: increment the bounded-retry counter and store the prior
    verify_cmd failure output (for fresh-re-dispatch prompt injection). Never
    writes state — the caller resets the task via task_transition."""
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET verify_retries = COALESCE(verify_retries, 0) + 1, "
            "verify_failure=?, updated_at=? WHERE id=? AND kind='task'",
            (failure, now, task_id),
        )
        conn.commit()


def set_task_diffstat(db, task_id: str, diffstat: str) -> None:
    """Pre-merge diffstat captured by integrate (hydration enrichment)."""
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET diffstat=?, updated_at=? WHERE id=? AND kind='task'",
            (diffstat, now, task_id),
        )
        conn.commit()


def set_task_topic(db, task_id: str, topic_id, conn=None) -> None:
    """Assign a task to its topic — writes the authoritative nodes.parent_id
    (get_task maps parent_id→topic_id). The legacy graph_tasks.topic_id write was
    cut (P8 c4-write-cut)."""
    with _cx(db, conn) as c:
        c.execute(
            "UPDATE nodes SET parent_id=? WHERE id=? AND kind='task'",
            (topic_id, task_id),
        )


def get_task(db, task_id: str, conn=None) -> dict | None:
    with _cx(db, conn) as c:
        row = c.execute(f"{_TASK_SELECT} AND id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def get_task_by_thread(db, thread_id: str) -> dict | None:
    with db._connect() as conn:
        row = conn.execute(
            f"{_TASK_SELECT} AND id IN (SELECT node_id FROM node_edges WHERE kind='dispatch' AND depends_on_id=?)", (thread_id,)
        ).fetchone()
    return dict(row) if row else None


def list_tasks(db, project_id: str) -> list[dict]:
    with db._connect() as conn:
        rows = conn.execute(
            f"{_TASK_SELECT} AND project_id=? ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# Dependency-edge CRUD (node_edges) extracted to db_graph_edges (LOC gate);
# re-exported below so ``from dbops.db_graph import get_deps`` keeps working.


# ── ready set ──────────────────────────────────────────────────────────────────


def ready_eligible(db, project_id: str) -> list[str]:
    """Open tasks of ``project_id`` whose deps are ALL 'verified'."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT n.id FROM nodes n WHERE n.kind='task' AND n.project_id=? "
            "AND n.state='open' AND NOT EXISTS ("
            "  SELECT 1 FROM node_edges e JOIN nodes d ON d.id=e.depends_on_id"
            "  WHERE e.node_id=n.id AND e.kind='dep' AND d.state != 'verified') "
            "ORDER BY n.created_at, n.id",
            (project_id,),
        ).fetchall()
        return [r["id"] for r in rows]


def recompute_ready(db, project_id: str) -> list[str]:
    """Promote every eligible 'open' task to 'ready'. Returns newly-ready ids.

    The promotion is a CAS (DA round-2 MAJOR-3, 2026-06-10): concurrent
    diamond fan-in completions both saw the join task eligible; the loser's
    read-then-write transition raised ValueError out of cmd_complete_agent.
    The conditional UPDATE makes a lost race a silent no-op (sanctioned
    writer #3 besides task_transition and the dispatcher's claim).
    """
    newly = []
    for task_id in ready_eligible(db, project_id):
        with _cx(db) as conn:
            won = cas_state(conn, task_id, frm="open", to="ready", now=_now())
        if won == 1:
            newly.append(task_id)
    if newly:
        try:
            from juggle_watchdog_poke import poke_watchdog
            poke_watchdog(db.db_path)
        except Exception:
            pass  # 30s backstop covers any poke failure
    return newly


# ── extracted seams (re-exported for back-compat) ─────────────────────────────

from dbops.db_graph_edges import (  # noqa: E402,F401
    get_dependents,
    get_deps,
    replace_edges,
    unverified_deps,
)
from dbops.db_graph_marking import (  # noqa: E402,F401
    mark_completion,
    mark_exec_failed,
    propagate_failure,
    recompute_blocked,
)
