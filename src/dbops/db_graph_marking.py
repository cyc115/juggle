"""dbops.db_graph_marking — completion marking + failure propagation.

Owns: mapping a thread completion's (integrate outcome, verify outcome) onto
the task state machine (``mark_completion``) and blocking the transitive
dependents of a failed task (``propagate_failure`` — design rev 2: no silent
stall, dependents go 'blocked-failed').
Must not own: the state machine itself or task CRUD (dbops.db_graph — every
state write here goes through its ``task_transition``), dispatching
(juggle_graph_dispatch), or notifications/action items
(juggle_cmd_agents_graph).

``dbops.db_graph`` re-exports these so callers keep a single graph-store
import seam.
"""

from __future__ import annotations

_ADVANCE_TO_INTEGRATING = {
    "open": ("deps_ready", "claim", "dispatch", "integrate_start"),
    "ready": ("claim", "dispatch", "integrate_start"),
    "dispatching": ("dispatch", "integrate_start"),
    "running": ("integrate_start",),
    "integrating": (),
}


def mark_completion(
    db, task_id: str, *, integrate_ok: bool, verify_ok: bool = True, handoff=None
) -> str:
    """Map a thread completion onto the task state machine. Returns final state.

    Walks the task legally to 'integrating', then applies the outcome:
    integrate failure → 'failed-integration' (DA B3: NEVER 'verified'),
    verify failure → 'failed-verify', else → 'verified'. Raises ValueError
    if the task is in a terminal/blocked state (fail loud, no silent remap).
    """
    # Deferred import: db_graph re-exports this module's functions at its
    # bottom, so a module-level import here would be import-order sensitive.
    from dbops.db_graph import get_task, task_transition, set_task_handoff

    task = get_task(db, task_id)
    if task is None:
        raise ValueError(f"graph task not found: {task_id!r}")
    state = task["state"]
    if state not in _ADVANCE_TO_INTEGRATING:
        raise ValueError(
            f"cannot mark completion: task {task_id!r} in terminal state {state!r}"
        )
    if handoff is not None:
        set_task_handoff(db, task_id, handoff)
    for event in _ADVANCE_TO_INTEGRATING[state]:
        task_transition(db, task_id, event)
    if not integrate_ok:
        return task_transition(db, task_id, "integrate_fail")
    if not verify_ok:
        return task_transition(db, task_id, "verify_fail")
    return task_transition(db, task_id, "integrate_ok")


_ADVANCE_TO_RUNNING = {
    "open": ("deps_ready", "claim", "dispatch"),
    "ready": ("claim", "dispatch"),
    "dispatching": ("dispatch",),
    "running": (),
}


def mark_exec_failed(db, task_id: str) -> str:
    """Walk the task legally to 'running', then apply 'exec_fail'.

    DA round-2 MAJOR-1 (2026-06-10): agent death (cmd_fail_agent / watchdog
    give-up) never reached the graph — the task stayed 'running' and its
    dependents stalled silently. Also serves the dispatch retry cap
    (dispatching → failed-exec). Raises ValueError on terminal / integrating
    tasks (fail loud, no silent remap).
    """
    from dbops.db_graph import get_task, task_transition

    task = get_task(db, task_id)
    if task is None:
        raise ValueError(f"graph task not found: {task_id!r}")
    state = task["state"]
    if state not in _ADVANCE_TO_RUNNING:
        raise ValueError(
            f"cannot mark exec failure: task {task_id!r} in state {state!r}"
        )
    for event in _ADVANCE_TO_RUNNING[state]:
        task_transition(db, task_id, event)
    return task_transition(db, task_id, "exec_fail")


_BLOCKING_STATES = frozenset(
    {"failed-exec", "failed-integration", "failed-verify", "blocked-failed"}
)


def recompute_blocked(db, project_id: str) -> tuple[list[str], list[str]]:
    """Re-derive blocked-failed from current dep states (after a spec reload).

    DA round-2 BLOCKER-1 (2026-06-10): reloading a fixed spec resurrected the
    failed task but its blocked-failed dependents stayed dead forever (no
    transition out of blocked-failed). Invariant restored here: a task is
    blocked-failed IFF some direct dep is failed-*/blocked-failed. Fixpoint:
      * blocked-failed task with NO blocking dep  → 'reload'   → open
      * open task WITH a blocking dep             → 'dep_fail' → blocked-failed
        (covers a blocked task whose content was edited: the load loop reloads
        it to open while one of its deps is still failed)
    The graph is a DAG and failed-* roots are fixed during the loop, so the
    fixpoint is unique and the loop terminates. Returns (unblocked, reblocked).
    """
    from dbops.db_graph import get_deps, get_task, list_tasks, task_transition

    unblocked: list[str] = []
    reblocked: list[str] = []
    changed = True
    while changed:
        changed = False
        for task in list_tasks(db, project_id):
            if task["state"] not in ("blocked-failed", "open"):
                continue
            deps = [get_task(db, d) for d in get_deps(db, task["id"])]
            blocking = any(d and d["state"] in _BLOCKING_STATES for d in deps)
            if task["state"] == "blocked-failed" and not blocking:
                task_transition(db, task["id"], "reload")
                unblocked.append(task["id"])
                changed = True
            elif task["state"] == "open" and blocking:
                task_transition(db, task["id"], "dep_fail")
                reblocked.append(task["id"])
                changed = True
    return unblocked, reblocked


def propagate_failure(db, task_id: str) -> list[str]:
    """Block ALL transitive dependents of a failed task. Returns blocked ids.

    Design rev 2 / Phase 3 (2026-06-10): a task in failed-exec |
    failed-integration | failed-verify must not leave its downstream silently
    'open' forever. BFS over the dependents closure; every task still in
    'open' or 'ready' transitions via 'dep_fail' → 'blocked-failed'
    (through the sole state writer). Idempotent: already-blocked/terminal
    dependents are skipped, so diamond shapes block each task exactly once
    and re-propagation returns []. Siblings (non-dependents) are untouched.
    """
    from dbops.db_graph import get_dependents, get_task, task_transition

    blocked: list[str] = []
    seen = {task_id}
    queue = [task_id]
    while queue:
        for dep_id in get_dependents(db, queue.pop(0)):
            if dep_id in seen:
                continue
            seen.add(dep_id)
            task = get_task(db, dep_id)
            if task and task["state"] in ("open", "ready"):
                task_transition(db, dep_id, "dep_fail")
                blocked.append(dep_id)
            queue.append(dep_id)
    return blocked
