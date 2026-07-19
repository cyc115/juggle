"""juggle_cmd_agents_adhoc_wrapper — RC2 (2026-07-19 stuck-in-background
incident) auto-wrap for a plain worktree thread.

Extracted from juggle_cmd_agents_graph_topics (LOC gate, 2026-07-19): owns
ensure_adhoc_topic_wrapper and propagate_wrapper_outcome_to_task. Must not own
the detached-integrate decision (juggle_cmd_agents_graph_topics.
start_detached_integrate) nor topic state semantics (dbops.db_topics) — this
module only ever creates the wrapper's initial rows and mirrors its resolved
outcome onto a real bound task; all state transitions run through the same
machinery a real graph topic / task uses.
"""

from __future__ import annotations

ADHOC_TOPIC_PREFIX = "adhoc-"


def is_adhoc_wrapper_topic(topic_id: str | None) -> bool:
    """True iff ``topic_id`` names a synthetic wrapper this module created
    (never a real user/graph topic — those never use this id shape)."""
    return bool(topic_id) and topic_id.startswith(ADHOC_TOPIC_PREFIX)


def ensure_adhoc_topic_wrapper(db, thread, thread_uuid) -> None:
    """A plain worktree thread has no db_topics topic to carry it through the
    safe detached-integrate + 'integrating' + reintegrate-sweep path RC1 built
    for graph topics — it fell to the INLINE _run_integrate call instead,
    which runs the full fetch/rebase/test-suite/merge/push synchronously
    inside whichever process applies its agent_complete event. When that
    event is SPOOLED (the normal case — a dispatched coder completes from its
    own process), the applying process is the WATCHDOG's own tick: the exact
    RC1 hazard (self-restart on HEAD-advance / tickguard hang-kill can die
    mid-gate), except a legacy thread had nothing to retry it afterward — no
    'integrating' promotion, and the reintegrate sweep only ever scans
    db_topics rows, so a killed inline merge wedged forever with no automatic
    recovery.

    Wraps the thread in a thin, pre-verified synthetic topic (one task,
    already 'verified') so start_detached_integrate treats it identically to
    a real graph topic — same detached spawn, same 'integrating' state, same
    (already-working, unmodified) reintegrate sweep lands it later.
    Idempotent: a no-op once the thread is bound to ANY topic (a real one or
    a previously-created wrapper — never re-wraps or double-creates).

    Bug#1-still-broken (2026-07-19): this now ALSO wraps a thread bound
    to a real graph TASK with no topic — the "true legacy"/project-dispatched
    shape (KO/KN/KU/KV/KS), which 4ddd742 excluded to avoid hijacking the
    task's own state (breaking DA B3's failed-integration pin), but that
    exclusion left EVERY project-dispatched coder thread on the fragile
    inline path — the exact case that matters (real work), never fixed. The
    task is NOT hijacked: it keeps its own dispatch edge on the same thread,
    and once the wrapper topic's real outcome is known,
    propagate_wrapper_outcome_to_task mirrors that SAME verdict onto it
    (never verified before the wrapper proves 'verified' — DA B3 preserved
    through the new async seam)."""
    from dbops import db_topics
    from dbops.db_topics_worktree_branch import (
        set_topic_main_repo_path, set_topic_worktree_branch,
    )

    if db_topics.get_topic_by_thread(db, thread_uuid):
        return
    topic_id = f"{ADHOC_TOPIC_PREFIX}{thread_uuid[:8]}"
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
    from dbops import db_graph

    task_id = f"{topic_id}-t0"
    if db_graph.get_task(db, task_id) is None:
        db_graph.create_task(
            db, task_id=task_id, project_id="INBOX", title=task_id,
            prompt="ad-hoc worktree finalize",
        )
        db_graph.set_task_topic(db, task_id, topic_id)
        db_graph.mark_completion(db, task_id, integrate_ok=True, verify_ok=True)


def propagate_wrapper_outcome_to_task(db, topic_id, thread_uuid, state, handoff, session_id) -> None:
    """Bug#1-still-broken: once a WRAPPER topic (see ensure_adhoc_topic_wrapper) reaches a
    terminal outcome, mirror that SAME verdict onto any real graph task bound
    to the same thread — the thread keeps its own dispatch edge on the real
    task independent of the wrapper's dispatch edge on the synthetic topic,
    so juggle_cmd_agents_graph.mark_graph_task can still find it.

    No-op for a real (non-wrapper) topic, a still-in-flight/async-pending
    state, or a thread with no bound task (mark_graph_task's own guard) —
    never raises."""
    from dbops.terminal_states import ASYNC_PENDING_STATES

    if not is_adhoc_wrapper_topic(topic_id) or not thread_uuid:
        return
    if state in ASYNC_PENDING_STATES:
        return  # not terminal yet — nothing to propagate
    _OUTCOME = {
        "verified": (True, False),
        "failed-verify": (True, True),
        "failed-integration": (False, False),
    }
    outcome = _OUTCOME.get(state)
    if outcome is None:
        return  # not a recognized wrapper-completion outcome (e.g. still
        # 'integrating', or 'failed-exec'/'delivered' — states a merge-
        # delivery wrapper never reaches) — leave the real task untouched
    integrate_ok, verify_failed = outcome
    from juggle_cmd_agents_graph import mark_graph_task

    mark_graph_task(
        db, thread_uuid, integrate_ok, handoff, session_id,
        verify_failed=verify_failed,
    )
