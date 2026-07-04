"""juggle_cmd_agents_graph_topics — TOPIC completion/failure glue (R9 Task 7).

Extracted from juggle_cmd_agents_graph (2026-06-11, LOC gate): the topic twins
of the task completion glue. The TOPIC owns the thread; its tasks are marked
per-task via the task machine (graph mark-task) and the topic finishes ONCE.

Owns: the A10 completion gate (check_topic_completion_gate + enforce_topic_gate),
mark_graph_topic (maps integrate/verify outcomes onto the TOPIC machine; falls
back to mark_graph_task for legacy task-bound threads), and fail_graph_topic
(agent death → topic failed-exec + dependent blocking, per-task states preserved).
Must not own: task glue (juggle_cmd_agents_graph), topic state semantics
(dbops.db_topics), completion/failure handlers (juggle_cmd_agents_complete).
"""

from __future__ import annotations

import sys

from dbops import event_kinds as _ek
from dbops.terminal_states import TASK_TERMINAL_STATES as _TASK_TERMINAL


def check_topic_completion_gate(db, thread_uuid) -> str | None:
    """R9/A10 gate: complete-agent on a topic thread refuses while any task is
    non-terminal. Returns refusal message or None. MUST run BEFORE integrate."""
    from dbops import db_topics

    try:
        topic = db_topics.get_topic_by_thread(db, thread_uuid)
    except Exception:
        return None  # pre-migration DB
    if not topic:
        return None  # not a topic thread — normal completion
    open_tasks = [n["id"] for n in db_topics.list_topic_tasks(db, topic["id"])
                  if n["state"] not in _TASK_TERMINAL]
    if open_tasks:
        return (
            f"topic {topic['id']} has unmarked task(s): {', '.join(open_tasks)} "
            f"— mark each with `juggle graph mark-task <id> --handoff '…'` "
            f"(or --fail) before complete-agent. Nothing was marked or merged."
        )
    return None


def enforce_topic_gate(db, thread_uuid) -> None:
    """Print + exit(1) when the topic gate refuses (no side effects yet)."""
    msg = check_topic_completion_gate(db, thread_uuid)
    if msg:
        print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)


def start_detached_integrate(db, thread_uuid, handoff) -> bool:
    """Complete-time detached-integrate handoff (2026-07-04 inline-gate death by
    watchdog respawn, integrate-wedge #2): mark the thread's bound TOPIC
    'integrating', spawn the SAME detached integrate the re-integrate sweep uses,
    and return — NEVER running the merge gate inline. The watchdog reconcile/
    reintegrate tick applies the final verdict from git reality.

    Only the integrate-worthy SUCCESS case is diverted — a topic whose member
    tasks are ALL 'verified' (the incident shape: work done, ready to land). A
    topic with a failed/incomplete task has nothing to land: the caller's inline
    mark_graph_topic drives it straight to 'failed-verify' + blocks dependents
    (cheap DB marking, no merge → no wedge risk), unchanged.

    Returns True when a TOPIC was marked 'integrating' + a detached integrate
    spawned (the caller must NOT finalize inline nor mark the outcome), False for
    a legacy non-topic thread OR a non-integrate-worthy topic (caller keeps its
    inline path)."""
    from dbops import db_topics
    from dbops.db_topics_marking import mark_topic_integrating
    from juggle_integrate_spawn import spawn_detached_integrate

    try:
        topic = db_topics.get_topic_by_thread(db, thread_uuid)
    except Exception:
        return False
    if not topic:
        return False  # legacy flat/adhoc thread — caller finalizes inline
    # Only divert an in-flight topic. A terminal/reopen state (e.g. already
    # 'verified' on a double-complete race) falls to the inline mark_graph_topic
    # path, which is idempotent + warns on an illegal transition — never crash
    # complete-agent by raising out of mark_topic_integrating.
    if topic["state"] not in ("open", "ready", "dispatching", "running", "integrating"):
        return False
    tasks = db_topics.list_topic_tasks(db, topic["id"])
    all_verified = bool(tasks) and all(n["state"] == "verified" for n in tasks)
    if not all_verified:
        return False  # failure/incomplete → caller marks failed-verify inline
    # Walk the topic to 'integrating' so the reintegrate sweep + reconcile own it.
    mark_topic_integrating(db, topic["id"], handoff=handoff)
    thread = db.get_thread(thread_uuid) or {}
    spawn_detached_integrate(thread, db)  # detached; outcome reconciles on a later tick
    return True


def finalize_or_detach_integrate(db, thread, thread_uuid, handoff):
    """Complete-time worktree finalize. A bound integrate-worthy TOPIC hands the
    merge to a DETACHED integrate (RC1 2026-07-04 inline-gate death by watchdog
    respawn — never run the gate inline in the watchdog/spool process) and returns
    detached=True; a legacy worktree thread finalizes inline via _run_integrate
    (accessed through juggle_cmd_agents_common so test monkeypatches take effect),
    and a pre-migration thread via bare _finalize_worktree. Returns
    (ft_success, ft_msg, detached)."""
    import juggle_cmd_agents_common as _com

    has_wt = bool(thread.get("worktree_path") and thread.get("worktree_branch")
                  and thread.get("main_repo_path"))
    if has_wt and start_detached_integrate(db, thread_uuid, handoff):
        return True, "integrate spawned detached (watchdog-owned)", True
    if has_wt:
        return (*_com.juggle_cmd_integrate._run_integrate(thread, db), False)
    return (*_com._finalize_worktree(thread), False)


def mark_graph_topic(db, thread_uuid, integrate_ok, handoff, session_id,
                     *, verify_failed=False, topic_id=None):
    """Topic twin of mark_graph_task: map (integrate, verify) outcomes onto the
    TOPIC machine; verify_ok additionally requires every task 'verified'.
    Falls back to mark_graph_task for legacy task-bound threads.

    ``topic_id`` names the EXACT topic to mark (fix-conflict-envelope-routing,
    2026-07-04 cyc_GP): a dual-dispatch thread owns several topics, so the
    reintegrate sweep must route each failed topic by id rather than let
    get_topic_by_thread guess the thread's first topic (which routed the same
    topic twice and left its integrating sibling wedged)."""
    from dbops import db_topics
    from juggle_cmd_agents_graph import mark_graph_task

    try:
        if topic_id is not None:
            topic = db_topics.get_topic(db, topic_id)
        else:
            topic = db_topics.get_topic_by_thread(db, thread_uuid)
    except Exception:
        return  # pre-migration DB without graph tables
    if not topic:
        return mark_graph_task(db, thread_uuid, integrate_ok, handoff,
                               session_id, verify_failed=verify_failed)
    tasks = db_topics.list_topic_tasks(db, topic["id"])
    all_verified = bool(tasks) and all(n["state"] == "verified" for n in tasks)
    try:
        state = db_topics.mark_topic_completion(
            db, topic["id"],
            integrate_ok=integrate_ok or verify_failed,
            verify_ok=(not verify_failed) and all_verified,
            handoff=handoff,
        )
    except db_topics.UnmergedVerifyRefused as e:
        # Defect C (2026-07-01): integrate reported success but no merged_sha was
        # recorded (e.g. recorded before the push, against a stale origin/main) →
        # →verified is refused. Do NOT swallow silently: self-heal via reconcile,
        # which re-derives merged_sha from git reality and completes →verified.
        import logging
        _log = logging.getLogger("juggle-topic")
        _log.warning(
            "topic %s: verify refused (merged_sha unrecorded) — attempting "
            "reconcile self-heal: %s", topic["id"], e,
        )
        try:
            state = db_topics.reconcile_topic_state(db, topic["id"])
        except Exception:
            _log.exception("topic %s: reconcile self-heal failed", topic["id"])
            state = (db_topics.get_topic(db, topic["id"]) or {}).get("state", "integrating")
        if state != "verified":
            db.add_action_item(
                thread_id=None,
                message=(f"Topic {topic['id']} stuck at {state} after integrate: "
                         f"merged_sha could not be reconciled from git. Investigate."),
                type_="failure", priority="high",
            )
    except ValueError as e:
        print(f"Warning: graph topic {topic['id']} not marked — {e}")
        return
    # Ledger close (best-effort): pair the topic thread's open dispatch run with
    # its OUTPUT (handoff) + the captured diffstat; status mirrors integrate.
    try:
        _t = db_topics.get_topic(db, topic["id"]) or {}
        db.close_run(
            thread_uuid, output=handoff, diffstat=_t.get("diffstat"),
            status="completed" if state == "verified" else "failed",
        )
    except Exception:
        pass

    if state == "verified":
        db.emit_event(
            thread_id=thread_uuid,
            message=f"⬢ topic {topic['id']} verified (merged)",
            session_id=session_id,
            kind=_ek.TOPIC_STATUS,
        )
    else:
        blocked = db_topics.propagate_topic_failure(db, topic["id"])
        detail = (f" Dependent topics blocked: {', '.join(blocked)}."
                  if blocked else "")
        db.emit_event(
            thread_id=thread_uuid,
            message=f"⬢ topic {topic['id']} → {state}",
            session_id=session_id,
            kind=_ek.TOPIC_STATUS,
        )
        db.add_action_item(
            thread_id=None,
            message=(f"Topic {topic['id']} failed ({state}).{detail} Fix and "
                     f"reload the graph spec to resume."),
            type_="failure", priority="high",
        )
    for ready_id in db_topics.recompute_topic_ready(db, topic["project_id"]):
        rt = db_topics.get_topic(db, ready_id)
        title = rt["title"] if rt else ready_id
        db.emit_event(
            thread_id=None,
            message=f"⬢ topic ready: {ready_id} — {title}",
            session_id=session_id,
            kind=_ek.TOPIC_STATUS,
        )


def fail_graph_topic(db, thread_uuid, session_id, reason=None) -> bool:
    """Agent death on a TOPIC thread → topic failed-exec + derived-dependent
    blocking + HIGH action item. Per-task states are NOT touched (resume story,
    spec DA A9). Returns True if a topic was handled, False otherwise (caller
    falls back to the task path)."""
    from dbops import db_topics
    from juggle_cmd_agents_graph import _ENFORCEABLE_STATES

    try:
        topic = db_topics.get_topic_by_thread(db, thread_uuid)
    except Exception:
        return False
    if not topic:
        return False
    if topic["state"] not in _ENFORCEABLE_STATES:
        return True  # terminal topic — double-failure stays warn-only (no-op)
    try:
        state = db_topics.mark_topic_exec_failed(db, topic["id"])
    except ValueError as e:
        print(f"Warning: graph topic {topic['id']} not marked failed — {e}")
        return True
    blocked = db_topics.propagate_topic_failure(db, topic["id"])
    detail = (f"dependent topics blocked: {', '.join(blocked)}."
              if blocked else "fix before dependents can run.")
    cause = f" Cause: {reason}." if reason else ""
    db.emit_event(
        thread_id=thread_uuid,
        message=f"⬢ topic {topic['id']} → {state}",
        session_id=session_id,
        kind=_ek.TOPIC_STATUS,
    )
    db.add_action_item(
        thread_id=thread_uuid,
        message=f"⚠️ Topic {topic['id']} ended in {state} — {detail}{cause}",
        type_="failure", priority="high",
    )
    return True
