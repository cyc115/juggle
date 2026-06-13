"""juggle_cmd_agents_graph_topics — TOPIC completion/failure glue (R9 Task 7).

Extracted from juggle_cmd_agents_graph (2026-06-11, LOC gate): the topic twins
of the node completion glue. The TOPIC owns the thread; its tasks are marked
per-task via the node machine (graph mark-task) and the topic finishes ONCE.

Owns: the A10 completion gate (check_topic_completion_gate + enforce_topic_gate),
mark_graph_topic (maps integrate/verify outcomes onto the TOPIC machine; falls
back to mark_graph_node for legacy node-bound threads), and fail_graph_topic
(agent death → topic failed-exec + dependent blocking, per-task states preserved).
Must not own: node glue (juggle_cmd_agents_graph), topic state semantics
(dbops.db_topics), completion/failure handlers (juggle_cmd_agents_complete).
"""

from __future__ import annotations

import sys

_TASK_TERMINAL = frozenset(
    {"verified", "failed-exec", "failed-integration", "failed-verify",
     "blocked-failed"}
)


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


def mark_graph_topic(db, thread_uuid, integrate_ok, handoff, session_id,
                     *, verify_failed=False):
    """Topic twin of mark_graph_node: map (integrate, verify) outcomes onto the
    TOPIC machine; verify_ok additionally requires every task 'verified'.
    Falls back to mark_graph_node for legacy node-bound threads."""
    from dbops import db_topics
    from juggle_cmd_agents_graph import mark_graph_node

    try:
        topic = db_topics.get_topic_by_thread(db, thread_uuid)
    except Exception:
        return  # pre-migration DB without graph tables
    if not topic:
        return mark_graph_node(db, thread_uuid, integrate_ok, handoff,
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
        db.add_notification_v2(
            thread_id=thread_uuid,
            message=f"⬢ topic {topic['id']} verified (merged)",
            session_id=session_id,
        )
    else:
        blocked = db_topics.propagate_topic_failure(db, topic["id"])
        detail = (f" Dependent topics blocked: {', '.join(blocked)}."
                  if blocked else "")
        db.add_notification_v2(
            thread_id=thread_uuid,
            message=f"⬢ topic {topic['id']} → {state}",
            session_id=session_id,
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
        db.add_notification_v2(
            thread_id=None,
            message=f"⬢ topic ready: {ready_id} — {title}",
            session_id=session_id,
        )


def fail_graph_topic(db, thread_uuid, session_id, reason=None) -> bool:
    """Agent death on a TOPIC thread → topic failed-exec + derived-dependent
    blocking + HIGH action item. Per-task states are NOT touched (resume story,
    spec DA A9). Returns True if a topic was handled, False otherwise (caller
    falls back to the node path)."""
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
    db.add_notification_v2(
        thread_id=thread_uuid,
        message=f"⬢ topic {topic['id']} → {state}",
        session_id=session_id,
    )
    db.add_action_item(
        thread_id=thread_uuid,
        message=f"⚠️ Topic {topic['id']} ended in {state} — {detail}{cause}",
        type_="failure", priority="high",
    )
    return True
