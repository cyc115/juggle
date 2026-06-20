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

# Task state machine (design 2026-06-10 rev 2):
# pending → ready → dispatching → running → integrating → verified
# failure exits: failed-exec | failed-integration | failed-verify
# dependents of a failed task: blocked-failed (terminal in Phase 1)
VALID_STATES = frozenset(
    {
        "pending", "ready", "dispatching", "running", "integrating",
        "verified", "failed-exec", "failed-integration", "failed-verify",
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
    ("ready", "unready"): "pending",  # add-task --required-by demotes ready task
    ("dispatching", "dispatch"): "running",
    ("dispatching", "stale_reset"): "ready",
    ("running", "integrate_start"): "integrating",
    ("running", "exec_fail"): "failed-exec",
    ("integrating", "integrate_ok"): "verified",
    ("integrating", "integrate_fail"): "failed-integration",
    ("integrating", "verify_fail"): "failed-verify",
    # Re-load of an edited spec may resurrect failed tasks (guarded upsert).
    ("failed-exec", "reload"): "pending",
    ("failed-integration", "reload"): "pending",
    ("failed-verify", "reload"): "pending",
    # DA round-2 BLOCKER-1 (2026-06-10): without this, blocked-failed was a
    # dead end — the blocked tail could never resume after a spec reload.
    ("blocked-failed", "reload"): "pending",
}

_EVENTS = frozenset(ev for (_, ev) in _TRANSITIONS)

# Tasks in these states must not be modified by a re-load (guarded upsert).
PROTECTED_STATES = frozenset({"dispatching", "running", "integrating", "verified"})

# Tick-owned states (DA B5): a thread bound to a task in one of these is
# dispatched by the watchdog tick — manual send-task must refuse without
# --force-task. pending/failed-*/blocked-failed remain operator territory.
TICK_OWNED_STATES = frozenset(
    {"ready", "dispatching", "running", "integrating", "verified"}
)


# ── state machine ──────────────────────────────────────────────────────────────


def task_transition(db, task_id: str, event: str, conn=None) -> str:
    """Apply ``event`` to the task's state machine. The ONLY state writer.

    Returns the new state. Raises ValueError (fail loud) on an unknown task,
    unknown event, or illegal (state, event) pair — state is left untouched.
    """
    if event not in _EVENTS:
        raise ValueError(f"graph task event unknown: {event!r}")
    task = get_task(db, task_id, conn=conn)
    if task is None:
        raise ValueError(f"graph task not found: {task_id!r}")
    key = (task["state"], event)
    if key not in _TRANSITIONS:
        raise ValueError(
            f"illegal graph transition: task {task_id!r} in state "
            f"{task['state']!r} got event {event!r}"
        )
    new_state = _TRANSITIONS[key]
    now = _now()
    sets, params = ["state=?", "updated_at=?"], [new_state, now]
    if new_state == "verified":
        sets.append("verified_at=?")
        params.append(now)
    if event == "reload":
        # A resurrected task must not keep its dead thread's id (DA round-2
        # minor 4, 2026-06-10): stale bindings resolved to closed threads.
        sets.append("thread_id=NULL")
    with _cx(db, conn) as c:
        c.execute(
            f"UPDATE graph_tasks SET {', '.join(sets)} WHERE id=?",
            (*params, task_id),
        )
    return new_state


# ── CRUD (never writes state) ──────────────────────────────────────────────────


def create_task(
    db, *, task_id: str, project_id: str, title: str, prompt: str, verify_cmd=None,
    conn=None,
) -> None:
    """Insert a new task in state 'pending'. Raises on duplicate id."""
    now = _now()
    with _cx(db, conn) as c:
        c.execute(
            "INSERT INTO graph_tasks (id, project_id, title, prompt, verify_cmd, "
            "state, created_at, updated_at) VALUES (?,?,?,?,?, 'pending', ?, ?)",
            (task_id, project_id, title, prompt, verify_cmd, now, now),
        )


def update_task_content(
    db, task_id: str, *, title: str, prompt: str, verify_cmd, conn=None
) -> None:
    """Update plan content (title/prompt/verify_cmd). Never touches state."""
    with _cx(db, conn) as c:
        c.execute(
            "UPDATE graph_tasks SET title=?, prompt=?, verify_cmd=?, updated_at=? "
            "WHERE id=?",
            (title, prompt, verify_cmd, _now(), task_id),
        )


def set_task_thread(db, task_id: str, thread_id) -> None:
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_tasks SET thread_id=?, updated_at=? WHERE id=?",
            (thread_id, _now(), task_id),
        )
        conn.commit()


def set_task_handoff(db, task_id: str, handoff: str) -> None:
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_tasks SET handoff=?, updated_at=? WHERE id=?",
            (handoff, _now(), task_id),
        )
        conn.commit()


def set_task_diffstat(db, task_id: str, diffstat: str) -> None:
    """Pre-merge diffstat captured by integrate (hydration enrichment)."""
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_tasks SET diffstat=?, updated_at=? WHERE id=?",
            (diffstat, _now(), task_id),
        )
        conn.commit()


def get_task(db, task_id: str, conn=None) -> dict | None:
    with _cx(db, conn) as c:
        row = c.execute(
            "SELECT * FROM graph_tasks WHERE id=?", (task_id,)
        ).fetchone()
        return dict(row) if row else None


def get_task_by_thread(db, thread_id: str) -> dict | None:
    with db._connect() as conn:
        row = conn.execute(
            "SELECT * FROM graph_tasks WHERE thread_id=?", (thread_id,)
        ).fetchone()
    return dict(row) if row else None


def list_tasks(db, project_id: str) -> list[dict]:
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM graph_tasks WHERE project_id=? ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def replace_edges(db, task_id: str, dep_ids: list[str], conn=None) -> None:
    """Replace the full dependency list of ``task_id``."""
    with _cx(db, conn) as c:
        c.execute("DELETE FROM graph_edges WHERE task_id=?", (task_id,))
        c.executemany(
            "INSERT INTO graph_edges (task_id, depends_on_id) VALUES (?,?)",
            [(task_id, dep) for dep in dep_ids],
        )


def get_deps(db, task_id: str) -> list[str]:
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT depends_on_id FROM graph_edges WHERE task_id=? ORDER BY depends_on_id",
            (task_id,),
        ).fetchall()
        return [r["depends_on_id"] for r in rows]


def get_dependents(db, task_id: str) -> list[str]:
    """Task ids that depend on ``task_id`` (reverse edges)."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT task_id FROM graph_edges WHERE depends_on_id=? ORDER BY task_id",
            (task_id,),
        ).fetchall()
        return [r["task_id"] for r in rows]


def unverified_deps(db, task_id: str) -> list[str]:
    """Dep ids of ``task_id`` whose state is not 'verified' (blocking deps)."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT e.depends_on_id FROM graph_edges e "
            "JOIN graph_tasks d ON d.id = e.depends_on_id "
            "WHERE e.task_id=? AND d.state != 'verified' "
            "ORDER BY e.depends_on_id",
            (task_id,),
        ).fetchall()
        return [r["depends_on_id"] for r in rows]


# ── ready set ──────────────────────────────────────────────────────────────────


def ready_eligible(db, project_id: str) -> list[str]:
    """Pending tasks of ``project_id`` whose deps are ALL 'verified'."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT n.id FROM graph_tasks n WHERE n.project_id=? "
            "AND n.state='pending' AND NOT EXISTS ("
            "  SELECT 1 FROM graph_edges e JOIN graph_tasks d ON d.id=e.depends_on_id"
            "  WHERE e.task_id=n.id AND d.state != 'verified') "
            "ORDER BY n.created_at, n.id",
            (project_id,),
        ).fetchall()
        return [r["id"] for r in rows]


def recompute_ready(db, project_id: str) -> list[str]:
    """Promote every eligible pending task to 'ready'. Returns newly-ready ids.

    The promotion is a CAS (DA round-2 MAJOR-3, 2026-06-10): concurrent
    diamond fan-in completions both saw the join task eligible; the loser's
    read-then-write transition raised ValueError out of cmd_complete_agent.
    The conditional UPDATE makes a lost race a silent no-op (sanctioned
    writer #3 besides task_transition and the dispatcher's claim).
    """
    newly = []
    for task_id in ready_eligible(db, project_id):
        with _cx(db) as conn:
            cur = conn.execute(
                "UPDATE graph_tasks SET state='ready', updated_at=? "
                "WHERE id=? AND state='pending'",
                (_now(), task_id),
            )
        if cur.rowcount == 1:
            newly.append(task_id)
    if newly:
        try:
            from juggle_watchdog_poke import poke_watchdog
            poke_watchdog(db.db_path)
        except Exception:
            pass  # 30s backstop covers any poke failure
    return newly


# ── completion marking + failure propagation (extracted seam) ─────────────────

from dbops.db_graph_marking import (  # noqa: E402,F401
    mark_completion,
    mark_exec_failed,
    propagate_failure,
    recompute_blocked,
)
