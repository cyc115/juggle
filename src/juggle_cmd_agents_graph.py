"""
juggle_cmd_agents_graph — graph-task glue for the agent CLI (autopilot).

Owns: mark_graph_task (maps a thread completion's integrate outcome onto the
bound graph task + ready-set notifications/action items; notify ONLY —
dispatch is watchdog-owned, DA B4/M1), enforce_handoff_contract (DA M4:
complete-agent refuses tasks-with-dependents without --handoff), and
check_task_guard (DA B5: send-task refuses tick-owned tasks sans --force-task).
Must not own: completion/failure handlers (juggle_cmd_agents_complete) or
task state semantics (dbops.db_graph).
"""

from __future__ import annotations

import sys

# Task states where a completion is still meaningful (mirrors db_graph
# mark_completion's legal walk); terminal/blocked tasks skip enforcement —
# a double-completion stays the Phase 1 warn+no-op, never a refusal.
_ENFORCEABLE_STATES = frozenset(
    {"open", "ready", "dispatching", "running", "integrating"}
)


def close_adhoc_run(db, thread_uuid, result_summary) -> None:
    """Ledger close for AD-HOC (non-graph) completions (best-effort, NEVER
    breaks completion). Graph task/topic threads are closed by mark_graph_topic
    with the captured diffstat + integrate-aware status, so they are skipped
    here to avoid a premature completed-status close."""
    from dbops import db_graph, db_topics

    try:
        is_graph = bool(
            db_graph.get_task_by_thread(db, thread_uuid)
            or db_topics.get_topic_by_thread(db, thread_uuid)
        )
        if not is_graph:
            db.close_run(
                thread_uuid, output=result_summary, diffstat=None,
                status="completed",
            )
    except Exception:
        pass


def _task_for_thread(db, thread_uuid):
    """Bound task for a thread, or None (incl. pre-migration DBs)."""
    from dbops import db_graph

    try:
        return db_graph.get_task_by_thread(db, thread_uuid)
    except Exception:
        return None


def enforce_handoff_contract(db, thread_uuid, handoff) -> None:
    """DA M4: a graph task with dependents MUST hand off. Exits 1 on violation.

    Runs BEFORE any completion side effects — dependent prompts are hydrated
    from this handoff, so an empty one is garbage-in for every downstream task.
    """
    from dbops import db_graph

    task = _task_for_thread(db, thread_uuid)
    if not task or task["state"] not in _ENFORCEABLE_STATES:
        return
    if handoff and str(handoff).strip():
        return
    dependents = db_graph.get_dependents(db, task["id"])
    if not dependents:
        return
    print(
        f"Error: graph task {task['id']} has dependents ({', '.join(dependents)}) "
        f"which are hydrated from its handoff — re-run with "
        f"--handoff '<files touched, interfaces added/changed, key decisions, "
        f"follow-ups>'. Nothing was marked or closed."
    )
    sys.exit(1)


def check_task_guard(db, thread_uuid) -> str | None:
    """DA B5 (3-tier): manual CLI dispatch to a tick-protected thread is refused.

    TOPIC-bound thread in a protected state (dispatching/running/integrating/
    verified) → double-dispatch race risk. Returns an error string or None.
    The R8 armed-project guard is REMOVED (P7 — no per-project arming).
    This guard lives at the CLI layer only; the tick path (dispatch_node) does
    NOT go through this check.
    """
    from dbops import db_graph, db_topics

    if not thread_uuid:
        return None
    try:
        topic = db_topics.get_topic_by_thread(db, thread_uuid)
    except Exception:
        topic = None  # pre-migration DB
    bound = topic or _task_for_thread(db, thread_uuid)  # legacy task fallback
    if bound:
        if bound.get("is_mirror"):
            return None  # mirror tracker topics are never a dispatch conflict
        if bound["state"] not in db_graph.TICK_OWNED_STATES:
            return None  # operator territory — DA B5 unchanged
        kind = "topic" if topic else "task"
        return (
            f"thread is bound to graph {kind} {bound['id']} in tick-owned "
            f"state {bound['state']!r} — the autopilot watchdog tick "
            f"owns this node."
        )
    return None


def _notify_failure(db, task, state, thread_uuid, session_id, reason=None):
    """Failure aftermath shared by completion marking and agent death:
    Phase 3 propagation blocks ALL transitive dependents (blocked-failed) so
    the graph never silently stalls — the tick only claims 'ready' tasks, so
    blocked tasks are never dispatched — plus notification + HIGH action item.
    """
    from dbops import db_graph

    blocked = db_graph.propagate_failure(db, task["id"])
    db.add_notification_v2(
        thread_id=thread_uuid,
        message=f"⬢ graph task {task['id']} → {state}",
        session_id=session_id,
    )
    detail = (
        f"dependents blocked (blocked-failed): {', '.join(blocked)}. "
        f"Fix the task, then reload the graph spec to resume."
        if blocked
        else "fix before dependents can run."
    )
    cause = f" Cause: {reason}." if reason else ""
    db.add_action_item(
        thread_id=thread_uuid,
        message=f"⚠️ Graph task {task['id']} ended in {state} — {detail}{cause}",
        type_="failure",
        priority="high",
    )


def fail_graph_task(db, thread_uuid, session_id, reason=None):
    """Agent death → graph (DA round-2 MAJOR-1, 2026-06-10).

    cmd_fail_agent (unrecoverable) and the watchdog give-up path set thread
    status but never touched the bound task: it stayed 'running' (a PROTECTED
    state even reload refuses) and dependents stalled silently. Marks the task
    failed-exec via the legal walk, blocks dependents, raises the action item.
    No-op for unbound threads / terminal tasks (double-failure stays warn-only).

    Topic-aware (R9): delegates to fail_graph_topic first — a TOPIC-bound thread
    fails the TOPIC (per-task states preserved, spec DA A9). Falls back to the
    task path for legacy task-bound threads.
    """
    from dbops import db_graph
    from juggle_cmd_agents_graph_topics import fail_graph_topic

    if fail_graph_topic(db, thread_uuid, session_id, reason):
        return

    task = _task_for_thread(db, thread_uuid)
    if not task or task["state"] not in _ENFORCEABLE_STATES:
        return
    try:
        state = db_graph.mark_exec_failed(db, task["id"])
    except ValueError as e:
        print(f"Warning: graph task {task['id']} not marked failed — {e}")
        return
    _notify_failure(db, task, state, thread_uuid, session_id, reason=reason)


def mark_graph_task(db, thread_uuid, integrate_ok, handoff, session_id,
                    *, verify_failed=False):
    """If the thread is bound to a graph task, record the completion outcome.

    Maps (integrate outcome, verify outcome) → task event via
    dbops.db_graph.mark_completion: success → 'verified' (stored verified_at),
    verify_cmd failure → 'failed-verify' (DA M3: main untouched), any other
    integrate failure → 'failed-integration' (DA B3: never 'verified').
    Recomputes the ready set and emits a notification + action item per
    newly-ready task. NEVER dispatches.
    """
    from dbops import db_graph

    try:
        task = db_graph.get_task_by_thread(db, thread_uuid)
    except Exception:
        return  # pre-migration DB without graph tables — nothing to mark
    if not task:
        return

    try:
        state = db_graph.mark_completion(
            db,
            task["id"],
            # A verify failure is not an integrate failure: rebase + repo
            # tests were fine, the task's own predicate was red.
            integrate_ok=integrate_ok or verify_failed,
            verify_ok=not verify_failed,
            handoff=handoff,
        )
    except ValueError as e:
        print(f"Warning: graph task {task['id']} not marked — {e}")
        return

    if state == "verified":
        db.add_notification_v2(
            thread_id=thread_uuid,
            message=f"⬢ graph task {task['id']} verified",
            session_id=session_id,
        )
    else:
        _notify_failure(db, task, state, thread_uuid, session_id)

    for ready_id in db_graph.recompute_ready(db, task["project_id"]):
        ready_task = db_graph.get_task(db, ready_id)
        title = ready_task["title"] if ready_task else ready_id
        db.add_notification_v2(
            thread_id=None,
            message=f"⬢ graph task ready: {ready_id} — {title}",
            session_id=session_id,
        )
        db.add_action_item(
            thread_id=None,
            message=f"Graph task ready to dispatch: {ready_id} — {title}",
            type_="manual_step",
            priority="normal",
        )


# Topic completion/failure glue lives in juggle_cmd_agents_graph_topics (LOC
# gate); fail_graph_task delegates to fail_graph_topic, and the completion
# handler imports check_topic_completion_gate/enforce_topic_gate/mark_graph_topic
# from there directly.
