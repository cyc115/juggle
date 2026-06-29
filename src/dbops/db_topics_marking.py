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

from dbops.db_topics import get_topic, set_topic_handoff, topic_transition

_ADVANCE_TO_INTEGRATING = {
    "open": ("deps_ready", "claim", "dispatch", "integrate_start"),
    "ready": ("claim", "dispatch", "integrate_start"),
    "dispatching": ("dispatch", "integrate_start"),
    "running": ("integrate_start",),
    "integrating": (),
}


def mark_topic_completion(db, topic_id, *, integrate_ok, verify_ok=True,
                          handoff=None) -> str:
    """Topic twin of db_graph.mark_completion: walk legally to 'integrating',
    apply the outcome. verified-means-MERGED holds at topic level (spec §2.3).

    Idempotent for the success path: if the topic is already 'verified', return
    'verified' without raising. Prevents a task stuck at 'running' when an
    out-of-band integrate + a racing complete-agent both succeed (2026-06-11 bug I).
    """
    topic = get_topic(db, topic_id)
    if topic is None:
        raise ValueError(f"graph topic not found: {topic_id!r}")
    if topic["state"] == "verified" and integrate_ok:
        return "verified"
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
    return topic_transition(db, topic_id, "integrate_ok")


def mark_topic_exec_failed(db, topic_id) -> str:
    """Agent death / give-up: walk the topic legally to 'failed-exec'
    (mirror of db_graph.mark_exec_failed — read it and follow its walk)."""
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
    blocked: list[str] = []
    frontier = [topic_id]
    while frontier:
        cur = frontier.pop()
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT n.parent_id FROM node_edges e "
                "JOIN nodes n ON n.id = e.node_id "
                "JOIN nodes d ON d.id = e.depends_on_id "
                "WHERE d.parent_id=? AND n.parent_id != ?",
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
