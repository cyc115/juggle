"""dbops.db_topics_marking — topic completion / exec-failure / failure-propagation.

Split out of dbops.db_topics (P8 Task 4.2, LOC gate) — the topic twins of
dbops.db_graph_marking. They drive a topic legally to 'integrating'/'failed-exec'
via the sole writer (db_topics.topic_transition) and block transitive DERIVED
dependents over node_edges. Re-exported from dbops.db_topics so existing callers
keep importing `from dbops.db_topics import mark_topic_completion` etc.

Must not own: the topic state machine (db_topics.topic_transition), topic CRUD,
or the derive-and-sync reconcile (db_topics_reconcile).
"""

from __future__ import annotations

# db_topics is imported at FUNCTION level below, not here: this module and
# db_topics form a re-export cycle (db_topics's bottom does `from
# dbops.db_topics_marking import ...`). A top-level `from dbops.db_topics import
# ...` here made import order matter — whichever module loaded first left the
# other's names undefined, raising `ImportError: cannot import name
# 'mark_topic_completion' from partially initialized module`. Deferring the
# import (the same pattern unblock_topic_dependents / db_topics_reconcile use)
# breaks the cycle. Pinned by test_db_topics_marking_importable_before_db_topics.
from dbops.schema import _now


def set_topic_fail_envelope(db, topic_id, fail_envelope: str) -> None:
    """Persist the JSON fail envelope for a TOPIC-bound integrate refusal
    (irl-repair T4). The topic path integrates once as a unit and binds the
    thread to the TOPIC node (not a member task), so get_task_by_thread finds
    nothing and set_task_fail_envelope (kind='task') never fires — this is the
    topic-node counterpart the repair sweep reads to pick a playbook."""
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET fail_envelope=?, updated_at=? WHERE id=? AND kind='topic'",
            (fail_envelope, now, topic_id),
        )
        conn.commit()


def store_fail_envelope(db, thread_id, task, fail_envelope: str, *, topic_id=None) -> None:
    """Persist an integrate fail envelope where the T4 repair sweep can read it:
    on the bound graph TASK (flat-task path), else on the TOPIC(s) currently
    'integrating' on the thread. A non-graph thread (no task, no integrating
    topic) persists nothing. Best-effort — a Mock/pre-migration db resolving no
    topic simply skips.

    fix-conflict-envelope-routing (2026-07-04 cyc_GP): a dual-dispatch thread
    owns SEVERAL topics; the old get_topic_by_thread stamped the envelope on the
    thread's first topic (observed: an already-verified sibling), leaving the
    real integrating topics envelope-empty so the reintegrate sweep never routed
    them and re-drove the doomed gate forever. Stamp EVERY integrating topic on
    the thread (or the explicit ``topic_id`` a caller names) — never a
    thread-first-topic guess."""
    if task:
        from dbops import db_graph

        db_graph.set_task_fail_envelope(db, task["id"], fail_envelope)
        return
    if topic_id is not None:
        set_topic_fail_envelope(db, topic_id, fail_envelope)
        return
    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT e.node_id FROM node_edges e JOIN nodes n ON n.id=e.node_id "
                "WHERE e.kind='dispatch' AND e.depends_on_id=? AND n.kind='topic' "
                "AND n.state='integrating'",
                (thread_id,),
            ).fetchall()
    except Exception:
        rows = []
    for row in rows:
        set_topic_fail_envelope(db, row[0], fail_envelope)

_ADVANCE_TO_INTEGRATING = {
    "open": ("deps_ready", "claim", "dispatch", "integrate_start"),
    "ready": ("claim", "dispatch", "integrate_start"),
    "dispatching": ("dispatch", "integrate_start"),
    "running": ("integrate_start",),
    "integrating": (),
    # irl-repair T4: a watchdog repair agent dispatched into the preserved
    # worktree of a failed-integration topic re-runs integrate and completes
    # here. `reopen` (NOT `reload` — that clears the dispatch thread) resurrects
    # the topic while keeping its bound thread, then it walks up to integrating.
    # A re-failed integrate falls straight back to failed-integration.
    "failed-integration": ("reopen", "deps_ready", "claim", "dispatch", "integrate_start"),
}


def mark_topic_completion(db, topic_id, *, integrate_ok, verify_ok=True,
                          handoff=None, submitted_rev=None) -> str:
    """Topic twin of db_graph.mark_completion: walk legally to 'integrating',
    apply the outcome. verified-means-MERGED holds at topic level (spec §2.3).

    ``submitted_rev`` (SPEC §5.1/§5.2): pass the ticket/rev when the caller
    knows integrate only SUBMITTED the work to an async land queue (push_mode
    ="pr" or an async_land-capable backend) rather than landing it
    synchronously — the topic goes to 'integrated-unlanded' instead of racing
    the (fail-closed) verified gate, and merged_sha is never set. Omit (None)
    for the synchronous git-direct path, which keeps the pre-existing
    integrate_ok -> verified edge unchanged.

    Idempotent for the success paths: if the topic is already 'verified'/
    'integrated-unlanded' and integrate_ok, return that state without raising.
    Prevents a task stuck at 'running' when an out-of-band integrate + a
    racing complete-agent both succeed (2026-06-11 bug I).
    """
    from dbops.db_topics import (
        get_topic,
        set_topic_handoff,
        set_topic_submitted_rev,
        topic_transition,
    )

    topic = get_topic(db, topic_id)
    if topic is None:
        raise ValueError(f"graph topic not found: {topic_id!r}")
    if integrate_ok and topic["state"] in ("verified", "integrated-unlanded"):
        return topic["state"]
    if topic["state"] not in _ADVANCE_TO_INTEGRATING:
        raise ValueError(
            f"cannot mark completion: topic {topic_id!r} in terminal state "
            f"{topic['state']!r}"
        )
    if handoff is not None:
        set_topic_handoff(db, topic_id, handoff)
    for event in _ADVANCE_TO_INTEGRATING[topic["state"]]:
        topic_transition(db, topic_id, event)
    if not integrate_ok:
        return topic_transition(db, topic_id, "integrate_fail")
    if not verify_ok:
        return topic_transition(db, topic_id, "verify_fail")
    if submitted_rev:
        set_topic_submitted_rev(db, topic_id, submitted_rev)
        return topic_transition(db, topic_id, "integrate_submitted")
    return topic_transition(db, topic_id, "integrate_ok")


def mark_topic_integrating(db, topic_id, *, handoff=None) -> str:
    """Walk a topic legally to 'integrating' and STOP — no outcome applied.

    Used by complete-agent's detached-integrate handoff (2026-07-04 inline-gate
    death by watchdog respawn): the merge gate runs in a DETACHED subprocess, and
    the watchdog reconcile/reintegrate tick applies the final verdict from git
    reality. Stores ``handoff`` (mirrors mark_topic_completion). Idempotent when
    the topic is already 'integrating'."""
    from dbops.db_topics import get_topic, set_topic_handoff, topic_transition

    topic = get_topic(db, topic_id)
    if topic is None:
        raise ValueError(f"graph topic not found: {topic_id!r}")
    if handoff is not None:
        set_topic_handoff(db, topic_id, handoff)
    if topic["state"] == "integrating":
        return "integrating"
    if topic["state"] not in _ADVANCE_TO_INTEGRATING:
        raise ValueError(
            f"cannot mark integrating: topic {topic_id!r} in terminal state "
            f"{topic['state']!r}"
        )
    state = "integrating"
    for event in _ADVANCE_TO_INTEGRATING[topic["state"]]:
        state = topic_transition(db, topic_id, event)
    return state


def mark_topic_exec_failed(db, topic_id) -> str:
    """Agent death / give-up: walk the topic legally to 'failed-exec'
    (mirror of db_graph.mark_exec_failed — read it and follow its walk)."""
    from dbops.db_topics import get_topic, topic_transition

    topic = get_topic(db, topic_id)
    if topic is None:
        raise ValueError(f"graph topic not found: {topic_id!r}")
    walk = {"open": ("deps_ready", "claim", "dispatch"),
            "ready": ("claim", "dispatch"),
            "dispatching": ("dispatch",),
            "running": ()}
    if topic["state"] not in walk:
        raise ValueError(
            f"cannot mark exec-failed: topic {topic_id!r} in state "
            f"{topic['state']!r}"
        )
    for event in walk[topic["state"]]:
        topic_transition(db, topic_id, event)
    return topic_transition(db, topic_id, "exec_fail")


def propagate_topic_failure(db, topic_id) -> list[str]:
    """Block transitive DERIVED dependents of a failed topic (blocked-failed).
    Mirror of db_graph.propagate_failure over derived topic deps (node_edges)."""
    from dbops.db_topics import get_topic, topic_transition

    blocked: list[str] = []
    frontier = [topic_id]
    while frontier:
        cur = frontier.pop()
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT n.parent_id FROM node_edges e "
                "JOIN nodes n ON n.id = e.node_id "
                "JOIN nodes d ON d.id = e.depends_on_id "
                "WHERE e.kind='dep' AND d.parent_id=? AND n.parent_id != ?",
                (cur, cur),
            ).fetchall()
        for r in rows:
            dep_tid = r[0]
            t_ = get_topic(db, dep_tid)
            if t_ and t_["state"] in ("open", "ready"):
                topic_transition(db, dep_tid, "dep_fail")
                blocked.append(dep_tid)
                frontier.append(dep_tid)
    return blocked


_TOPIC_BLOCKING_STATES = frozenset(
    {"failed-exec", "failed-integration", "failed-verify", "blocked-failed"}
)


def _topic_has_blocking_dep(db, topic_id: str) -> bool:
    """True iff any CROSS-topic dependency of ``topic_id`` (a dep edge from one of
    its member tasks to a task owned by a DIFFERENT topic) resolves to a dep topic
    still in a blocking state. Mirrors db_topics.topic_ready_eligible's dep join
    and db_graph_marking.recompute_blocked's blocking predicate."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT dt.state FROM node_edges e "
            "JOIN nodes n ON n.id = e.node_id "
            "JOIN nodes d ON d.id = e.depends_on_id "
            "JOIN nodes dt ON dt.id = d.parent_id "
            "WHERE e.kind='dep' AND n.parent_id=? AND d.parent_id != ?",
            (topic_id, topic_id),
        ).fetchall()
    return any(r[0] in _TOPIC_BLOCKING_STATES for r in rows)


def unblock_topic_dependents(db, project_id: str) -> None:
    """Reverse any 'blocked-failed' topic whose blocking deps have ALL cleared,
    then re-derive ready. The INVERSE of propagate_topic_failure and the topic-
    level twin of db_graph_marking.recompute_blocked (T-fix-heal-deleted-branch,
    GAP 2): propagate_topic_failure blocked these dependents when their dep topic
    failed, and nothing but a spec reload ever reversed that — so a landed-then-
    lifted topic left its dependents dead. Fixpoint over the DAG (failed roots are
    fixed during the loop → terminates). Fail-soft: never raises into the caller."""
    from dbops.db_topics import list_topics, recompute_topic_ready, topic_transition

    try:
        changed = True
        while changed:
            changed = False
            for topic in list_topics(db, project_id):
                if topic["state"] != "blocked-failed":
                    continue
                if not _topic_has_blocking_dep(db, topic["id"]):
                    topic_transition(db, topic["id"], "reload")  # → open
                    changed = True
        recompute_topic_ready(db, project_id)
    except Exception:
        pass
