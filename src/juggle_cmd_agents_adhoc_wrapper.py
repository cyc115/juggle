"""juggle_cmd_agents_adhoc_wrapper — RC2 (2026-07-19 stuck-in-background
incident) auto-wrap for a plain worktree thread.

Extracted from juggle_cmd_agents_graph_topics (LOC gate, 2026-07-19): owns
ONLY ensure_adhoc_topic_wrapper. Must not own the detached-integrate decision
(juggle_cmd_agents_graph_topics.start_detached_integrate) nor topic state
semantics (dbops.db_topics) — this module only ever creates the wrapper's
initial rows, all further state transitions run through the same machinery a
real graph topic uses.
"""

from __future__ import annotations


def ensure_adhoc_topic_wrapper(db, thread, thread_uuid) -> None:
    """A plain (non-graph) worktree thread has no db_topics topic to carry it
    through the safe detached-integrate + 'integrating' + reintegrate-sweep
    path RC1 built for graph topics — it fell to the INLINE _run_integrate
    call instead, which runs the full fetch/rebase/test-suite/merge/push
    synchronously inside whichever process applies its agent_complete event.
    When that event is SPOOLED (the normal case — a dispatched coder
    completes from its own process), the applying process is the WATCHDOG's
    own tick: the exact RC1 hazard (self-restart on HEAD-advance / tickguard
    hang-kill can die mid-gate), except a legacy thread had nothing to retry
    it afterward — no 'integrating' promotion, and the reintegrate sweep only
    ever scans db_topics rows, so a killed inline merge wedged forever with
    no automatic recovery.

    Wraps the thread in a thin, pre-verified synthetic topic (one task,
    already 'verified') so start_detached_integrate treats it identically to
    a real graph topic — same detached spawn, same 'integrating' state, same
    (already-working, unmodified) reintegrate sweep lands it later.
    Idempotent: a no-op once the thread is bound to ANY topic (a real one or
    a previously-created wrapper — never re-wraps or double-creates). Also a
    no-op for a thread bound to a real graph TASK with no topic (the legacy
    task-only shape mark_graph_task already owns, e.g. DA B3's failed-
    integration pin) — same "is this thread graph-owned already" check as
    juggle_topic_lifecycle.reconcile_adhoc_integrate, so a task-bound thread's
    completion keeps routing through its own task machine, never hijacked
    into a synthetic topic that can't see (or update) that task's real state."""
    from dbops import db_graph, db_topics
    from dbops.db_topics_worktree_branch import (
        set_topic_main_repo_path, set_topic_worktree_branch,
    )

    if db_topics.get_topic_by_thread(db, thread_uuid):
        return
    if db_graph.get_task_by_thread(db, thread_uuid):
        return
    topic_id = f"adhoc-{thread_uuid[:8]}"
    db_topics.create_topic(  # INSERT OR IGNORE — idempotent
        db, topic_id=topic_id, project_id="INBOX",
        title=f"ad-hoc finalize {thread_uuid[:8]}",
    )
    db_topics.set_topic_thread(db, topic_id, thread_uuid)
    # Stamp the topic's OWN durable worktree_branch + main_repo_path NOW,
    # before the merge clears the thread's live fields — reconcile_topic_state
    # / merged-sha proof otherwise has nothing to fall back to once the
    # worktree is cleaned up.
    set_topic_worktree_branch(db, topic_id, (thread.get("worktree_branch") or "").strip())
    set_topic_main_repo_path(db, topic_id, (thread.get("main_repo_path") or "").strip())
    task_id = f"{topic_id}-t0"
    if db_graph.get_task(db, task_id) is None:
        db_graph.create_task(
            db, task_id=task_id, project_id="INBOX", title=task_id,
            prompt="ad-hoc worktree finalize",
        )
        db_graph.set_task_topic(db, task_id, topic_id)
        db_graph.mark_completion(db, task_id, integrate_ok=True, verify_ok=True)
