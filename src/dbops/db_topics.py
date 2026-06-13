"""dbops.db_topics — graph_topics store (3-tier autopilot, R9 2026-06-11).

Owns: topic CRUD, topic state transitions (REUSES db_graph's _TRANSITIONS —
one state machine, two tables, no second invention), DERIVED topic-level deps
(task edges crossing topic boundaries), the topic ready-set (CAS promote), and
topic completion marking.
Must not own: task semantics (dbops.db_graph), dispatching
(juggle_graph_dispatch — whose atomic topic claim is the sanctioned writer
besides topic_transition), CLI parsing.
"""

from __future__ import annotations

from dbops.schema import _now
from dbops.db_graph import _EVENTS, _TRANSITIONS, _cx


class UnmergedVerifyRefused(ValueError):
    """G1: a topic cannot be marked 'verified' while its work is unmerged."""


def _verified_allowed(db, topic_id: str) -> bool:
    """G1 gate: a topic may become 'verified' only when merged to main.

    Tests inject an isolated repo via thread.main_repo_path; when no repo is
    bound the topic is unmergeable → not allowed.
    """
    from dbops.graph_guards import topic_is_merged

    return topic_is_merged(db, topic_id)


def topic_transition(db, topic_id: str, event: str, conn=None) -> str:
    """Apply ``event`` to the topic. Same machine as tasks. Fail-loud."""
    if event not in _EVENTS:
        raise ValueError(f"graph topic event unknown: {event!r}")
    topic = get_topic(db, topic_id, conn=conn)
    if topic is None:
        raise ValueError(f"graph topic not found: {topic_id!r}")
    key = (topic["state"], event)
    if key not in _TRANSITIONS:
        raise ValueError(
            f"illegal graph transition: topic {topic_id!r} in state "
            f"{topic['state']!r} got event {event!r}"
        )
    new_state = _TRANSITIONS[key]
    if new_state == "verified" and not _verified_allowed(db, topic_id):
        raise UnmergedVerifyRefused(
            f"refusing to verify topic {topic_id!r}: its work is not merged into "
            f"main (git merge-base --is-ancestor failed). Keeping it pre-verified."
        )
    now = _now()
    sets, params = ["state=?", "updated_at=?"], [new_state, now]
    if new_state == "verified":
        sets.append("verified_at=?")
        params.append(now)
    if event == "reload":
        sets.append("thread_id=NULL")
    with _cx(db, conn) as c:
        c.execute(
            f"UPDATE graph_topics SET {', '.join(sets)} WHERE id=?",
            (*params, topic_id),
        )
    return new_state


def create_topic(db, *, topic_id, project_id, title, objective="", conn=None) -> None:
    now = _now()
    with _cx(db, conn) as c:
        c.execute(
            "INSERT INTO graph_topics (id, project_id, title, objective, state, "
            "created_at, updated_at) VALUES (?,?,?,?, 'pending', ?, ?)",
            (topic_id, project_id, title, objective, now, now),
        )


def get_topic(db, topic_id, conn=None) -> dict | None:
    with _cx(db, conn) as c:
        row = c.execute("SELECT * FROM graph_topics WHERE id=?", (topic_id,)).fetchone()
        return dict(row) if row else None


def get_topic_by_thread(db, thread_id) -> dict | None:
    with db._connect() as conn:
        row = conn.execute(
            "SELECT * FROM graph_topics WHERE thread_id=?", (thread_id,)
        ).fetchone()
    return dict(row) if row else None


def list_topics(db, project_id) -> list[dict]:
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM graph_topics WHERE project_id=? ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_topic_thread(db, topic_id, thread_id) -> None:
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_topics SET thread_id=?, updated_at=? WHERE id=?",
            (thread_id, _now(), topic_id),
        )
        conn.commit()


def set_topic_handoff(db, topic_id, handoff) -> None:
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_topics SET handoff=?, updated_at=? WHERE id=?",
            (handoff, _now(), topic_id),
        )
        conn.commit()


def list_topic_tasks(db, topic_id) -> list[dict]:
    """Tasks of a topic in intra-topic topological order (created_at,id ties).

    The topic agent executes tasks SEQUENTIALLY in this order (R9 hybrid)."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM graph_tasks WHERE topic_id=? ORDER BY created_at, id",
            (topic_id,),
        ).fetchall()
    tasks = [dict(r) for r in rows]
    ids = {n["id"] for n in tasks}
    if not ids:
        return []
    with db._connect() as conn:
        edges = conn.execute(
            "SELECT task_id, depends_on_id FROM graph_edges "
            "WHERE task_id IN (%s)" % ",".join("?" * len(ids)),
            tuple(ids),
        ).fetchall()
    deps = {n["id"]: set() for n in tasks}
    for e in edges:
        if e["depends_on_id"] in ids:  # intra-topic edges only order execution
            deps[e["task_id"]].add(e["depends_on_id"])
    ordered, emitted = [], set()
    pool = list(tasks)
    while pool:
        progressed = False
        for n in list(pool):
            if deps[n["id"]] <= emitted:
                ordered.append(n)
                emitted.add(n["id"])
                pool.remove(n)
                progressed = True
        if not progressed:  # cycle — load-time validation should prevent this
            ordered.extend(pool)
            break
    return ordered


def derived_topic_deps(db, topic_id) -> list[str]:
    """Topics this topic depends on: any task edge crossing the boundary."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT d.topic_id FROM graph_edges e "
            "JOIN graph_tasks n ON n.id = e.task_id "
            "JOIN graph_tasks d ON d.id = e.depends_on_id "
            "WHERE n.topic_id=? AND d.topic_id IS NOT NULL AND d.topic_id != ? "
            "ORDER BY d.topic_id",
            (topic_id, topic_id),
        ).fetchall()
    return [r[0] for r in rows]


_DISPATCHABLE_TASK_STATES = ("pending", "ready")


def topic_ready_eligible(db, project_id) -> list[str]:
    """Pending topics whose DERIVED dep topics are all 'verified' AND that have
    at least one task in a dispatchable state.

    G3 (claimable invariant): a topic with ZERO tasks in a dispatchable state
    (empty, or all tasks terminal/active) is never promoted to 'ready'. The
    2026-06-13 incident's empty-topic TOCTOU race claimed a topic created before
    its first task existed — gate that here at the source of truth.
    """
    placeholders = ",".join("?" * len(_DISPATCHABLE_TASK_STATES))
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT t.id FROM graph_topics t WHERE t.project_id=? "
            "AND t.state='pending' AND NOT EXISTS ("
            "  SELECT 1 FROM graph_edges e"
            "  JOIN graph_tasks n ON n.id = e.task_id"
            "  JOIN graph_tasks d ON d.id = e.depends_on_id"
            "  JOIN graph_topics dt ON dt.id = d.topic_id"
            "  WHERE n.topic_id = t.id AND d.topic_id != t.id"
            "  AND dt.state != 'verified') "
            "AND EXISTS ("
            "  SELECT 1 FROM graph_tasks gt"
            f"  WHERE gt.topic_id = t.id AND gt.state IN ({placeholders})) "
            "ORDER BY t.created_at, t.id",
            (project_id, *_DISPATCHABLE_TASK_STATES),
        ).fetchall()
        return [r["id"] for r in rows]


def recompute_topic_ready(db, project_id) -> list[str]:
    """CAS-promote eligible pending topics to 'ready' (same race discipline as
    db_graph.recompute_ready — a lost race is a silent no-op)."""
    newly = []
    for tid in topic_ready_eligible(db, project_id):
        with _cx(db) as conn:
            cur = conn.execute(
                "UPDATE graph_topics SET state='ready', updated_at=? "
                "WHERE id=? AND state='pending'",
                (_now(), tid),
            )
        if cur.rowcount == 1:
            newly.append(tid)
    return newly


_ADVANCE_TO_INTEGRATING = {
    "pending": ("deps_ready", "claim", "dispatch", "integrate_start"),
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
    walk = {"pending": ("deps_ready", "claim", "dispatch"),
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
    Mirror of db_graph.propagate_failure over derived topic deps."""
    blocked: list[str] = []
    frontier = [topic_id]
    while frontier:
        cur = frontier.pop()
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT n.topic_id FROM graph_edges e "
                "JOIN graph_tasks n ON n.id = e.task_id "
                "JOIN graph_tasks d ON d.id = e.depends_on_id "
                "WHERE d.topic_id=? AND n.topic_id != ?",
                (cur, cur),
            ).fetchall()
        for r in rows:
            dep_tid = r[0]
            t_ = get_topic(db, dep_tid)
            if t_ and t_["state"] in ("pending", "ready"):
                topic_transition(db, dep_tid, "dep_fail")
                blocked.append(dep_tid)
                frontier.append(dep_tid)
    return blocked


# Reconcile (derive-and-sync) lives in db_topics_reconcile (LOC split,
# 2026-06-13). Re-exported here so existing callers keep importing from
# dbops.db_topics. Imported at module end to avoid a circular import
# (db_topics_reconcile imports get_topic/list_topics/_verified_allowed from here).


def topic_counts(db, project_id) -> dict | None:
    """Display counts over graph_topics (same shape as juggle_graph_status)."""
    from juggle_graph_status import counts_from_states

    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT state FROM graph_topics WHERE project_id=?", (project_id,)
            ).fetchall()
    except Exception:
        return None  # pre-migration DB
    states = [r[0] for r in rows]
    return counts_from_states(states) if states else None


# Back-compat re-exports for the reconcile pass (LOC split, 2026-06-13). Placed
# at module end so db_topics is fully defined before db_topics_reconcile imports
# from it (avoids a circular-import failure at load time).
from dbops.db_topics_reconcile import (  # noqa: E402,F401
    reconcile_project_topics,
    reconcile_topic_state,
)
