"""dbops.db_topics — topic store over the unified nodes table (3-tier autopilot).

P8: topic reads resolve from ``nodes`` — a topic is a kind='topic' node
(parent_id IS NULL, topic-tier); its child tasks are kind='task' carrying
parent_id=<topic id>, and topic deps are derived from node_edges crossing parent
boundaries. The kind='topic' discriminator (P8 M2, Migration 53) replaced the
legacy graph_topics-membership anti-join; graph_topics is dual-written only until
the terminal drop retires it.

Owns: topic CRUD, topic state transitions (DELEGATES the decision to the unified
db_node_machine.node_transition — one state machine, no second invention), DERIVED topic-level deps
(task edges crossing topic boundaries), the topic ready-set (CAS promote), and
topic completion marking.
Must not own: task semantics (dbops.db_graph), dispatching
(juggle_graph_dispatch — whose atomic topic claim is the sanctioned writer
besides topic_transition), CLI parsing.
"""

from __future__ import annotations

from dbops.schema import _now
from dbops.db_graph import _cx
from dbops.db_node_machine import InvalidTransition, legal_events, node_transition
from dbops.state_write import cas_state, write_state

# Topic/task discriminator (P8 M2): a topic is its OWN kind='topic' node
# (Migration 53 promoted every graph_topics member; create_topic writes kind='topic'
# directly). This replaces the graph_topics-membership anti-join so the distinction
# survives the graph_topics drop. parent_id IS NULL alone was never sufficient — a
# bare task (no owning topic) is also parent_id-NULL, and mis-classifying it as a
# topic routes complete-agent through the topic G1-merge gate instead of the task
# path (2026-06-29 incident) — the kind discriminator removes that ambiguity.
_TOPIC_ONLY = "kind='topic'"

# nodes projection reproducing the legacy graph_topics row shape so topic-tier
# consumers keep their column names after the P8 read-flip (Task 4.2). Child tasks
# carry parent_id=<topic id>; topic deps derive from node_edges crossing parent
# boundaries. thread_id comes from the typed kind='dispatch' node_edge (P8 M1/Q2).
_TOPIC_SELECT = (
    "SELECT id, project_id, title, objective, state, "
    "(SELECT depends_on_id FROM node_edges WHERE node_id=nodes.id AND kind='dispatch' "
    "LIMIT 1) AS thread_id, merged_sha, handoff, diffstat, "
    f"verified_at, priority, created_at, updated_at FROM nodes WHERE {_TOPIC_ONLY}"
)


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
    """Apply ``event`` via the unified machine (node_transition, kind='task'). Fail-loud."""
    if event not in legal_events("task"):
        raise ValueError(f"graph topic event unknown: {event!r}")
    topic = get_topic(db, topic_id, conn=conn)
    if topic is None:
        raise ValueError(f"graph topic not found: {topic_id!r}")
    try:
        new_state = node_transition(topic["state"], event, "task")
    except InvalidTransition as e:
        raise ValueError(
            f"illegal graph transition: topic {topic_id!r} in state "
            f"{topic['state']!r} got event {event!r}"
        ) from e
    if new_state == "verified" and not _verified_allowed(db, topic_id):
        raise UnmergedVerifyRefused(
            f"refusing to verify topic {topic_id!r}: its work is not merged into "
            f"main (git merge-base --is-ancestor failed). Keeping it pre-verified."
        )
    # Single lockstep writer: nodes + graph_topics together (M3).
    with _cx(db, conn) as c:
        write_state(c, topic_id, new_state, now=_now(),
                    verified=(new_state == "verified"),
                    clear_thread=(event == "reload"))
    return new_state


def create_topic(
    db, *, topic_id, project_id, title, objective="", priority: int = 0, conn=None
) -> None:
    """Insert a topic as an authoritative kind='topic' node (parent_id NULL =
    topic-tier). The kind discriminator (P8 M2) is what separates topics from bare
    tasks. Writes ONLY nodes — the legacy graph_topics INSERT was cut
    (P8 c4-write-cut). ``priority`` (default 0) lifts a fix/defect topic ahead in
    the ready-dispatch interleave."""
    now = _now()
    with _cx(db, conn) as c:
        c.execute(
            "INSERT OR IGNORE INTO nodes (id, kind, title, objective, state, "
            "project_id, parent_id, priority, created_at, updated_at) "
            "VALUES (?, 'topic', ?, ?, 'open', ?, NULL, ?, ?, ?)",
            (topic_id, title, objective, project_id, priority, now, now),
        )


def get_topic(db, topic_id, conn=None) -> dict | None:
    with _cx(db, conn) as c:
        row = c.execute(f"{_TOPIC_SELECT} AND id=?", (topic_id,)).fetchone()
        return dict(row) if row else None


def get_topic_by_thread(db, thread_id) -> dict | None:
    with db._connect() as conn:
        row = conn.execute(
            f"{_TOPIC_SELECT} AND id IN "
            "(SELECT node_id FROM node_edges WHERE kind='dispatch' AND depends_on_id=?)",
            (thread_id,),
        ).fetchone()
    return dict(row) if row else None


def list_topics(db, project_id) -> list[dict]:
    """Real graph topics for a project, in topological-ish (created_at,id) order.

    Topics are kind='topic' nodes (parent_id IS NULL). Conversation
    nodes (kind='conversation') — including a chat thread `project assign`-ed to a
    project — are excluded by the kind discriminator, so they never surface as a
    phantom graph node (the 2026-06-15 mirror-projection defect can no longer
    occur: the conversation IS a node, never a graph_topics projection)."""
    with db._connect() as conn:
        rows = conn.execute(
            f"{_TOPIC_SELECT} AND project_id=? ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_topic_thread(db, topic_id, thread_id) -> None:
    """Bind the dispatch thread as a typed kind='dispatch' node_edge (P8 M1/Q2;
    thread_id=None unbinds), feeding the flipped get_topic_by_thread / cockpit-DAG
    / orphan_guard readers. Touches nodes.updated_at and (compat) graph_topics."""
    from dbops.dispatch_edge import bind_dispatch_thread

    now = _now()
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET updated_at=? WHERE id=? AND kind='topic'",
            (now, topic_id),
        )
        bind_dispatch_thread(conn, topic_id, thread_id)
        conn.commit()


def set_topic_merged_sha(db, topic_id, merged_sha, conn=None) -> None:
    """Record the merge commit (branch tip now on main) for ``topic_id``.

    The single source of truth for the verified gate (T-verified-merged-sha):
    integrate writes this on a successful ff-merge/push so the topic can verify.
    Writes ONLY nodes.merged_sha — the legacy graph_topics UPDATE was cut
    (P8 c4-write-cut).
    """
    now = _now()
    with _cx(db, conn) as c:
        c.execute(
            "UPDATE nodes SET merged_sha=?, updated_at=? WHERE id=? AND kind='topic'",
            (merged_sha, now, topic_id),
        )


def set_topic_handoff(db, topic_id, handoff) -> None:
    """Record the topic handoff on the authoritative nodes.handoff — the legacy
    graph_topics UPDATE was cut (P8 c4-write-cut)."""
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET handoff=?, updated_at=? WHERE id=? AND kind='topic'",
            (handoff, now, topic_id),
        )
        conn.commit()


def list_topic_tasks(db, topic_id) -> list[dict]:
    """Tasks of a topic in intra-topic topological order (created_at,id ties).

    The topic agent executes tasks SEQUENTIALLY in this order (R9 hybrid)."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id, project_id, title, objective AS prompt, verify_cmd, state, "
            "(SELECT depends_on_id FROM node_edges WHERE node_id=nodes.id "
            "AND kind='dispatch' LIMIT 1) AS thread_id, "
            "parent_id AS topic_id, handoff, "
            "diffstat, verified_at, created_at, updated_at "
            "FROM nodes WHERE kind='task' AND parent_id=? ORDER BY created_at, id",
            (topic_id,),
        ).fetchall()
    tasks = [dict(r) for r in rows]
    ids = {n["id"] for n in tasks}
    if not ids:
        return []
    with db._connect() as conn:
        edges = conn.execute(
            "SELECT node_id AS task_id, depends_on_id FROM node_edges "
            "WHERE kind='dep' AND node_id IN (%s)" % ",".join("?" * len(ids)),
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
            "SELECT DISTINCT d.parent_id FROM node_edges e "
            "JOIN nodes n ON n.id = e.node_id "
            "JOIN nodes d ON d.id = e.depends_on_id "
            "WHERE e.kind='dep' AND n.parent_id=? "
            "AND d.parent_id IS NOT NULL AND d.parent_id != ? "
            "ORDER BY d.parent_id",
            (topic_id, topic_id),
        ).fetchall()
    return [r[0] for r in rows]


_DISPATCHABLE_TASK_STATES = ("open", "ready")


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
            "SELECT t.id FROM nodes t "
            "WHERE t.kind='topic' "
            "AND t.project_id=? "
            "AND t.state='open' AND NOT EXISTS ("
            "  SELECT 1 FROM node_edges e"
            "  JOIN nodes n ON n.id = e.node_id"
            "  JOIN nodes d ON d.id = e.depends_on_id"
            "  JOIN nodes dt ON dt.id = d.parent_id"
            "  WHERE e.kind='dep' AND n.parent_id = t.id AND d.parent_id != t.id"
            "  AND dt.state != 'verified') "
            "AND EXISTS ("
            "  SELECT 1 FROM nodes gt"
            f"  WHERE gt.parent_id = t.id AND gt.state IN ({placeholders})) "
            "ORDER BY t.created_at, t.id",
            (project_id, *_DISPATCHABLE_TASK_STATES),
        ).fetchall()
        return [r["id"] for r in rows]


def recompute_topic_ready(db, project_id) -> list[str]:
    """CAS-promote eligible 'open' topics to 'ready' (same race discipline as
    db_graph.recompute_ready — a lost race is a silent no-op). The CAS writes
    ``nodes`` (authoritative) in lockstep with the legacy graph_topics row."""
    newly = []
    for tid in topic_ready_eligible(db, project_id):
        with _cx(db) as conn:
            won = cas_state(conn, tid, frm="open", to="ready", now=_now())
        if won == 1:
            newly.append(tid)
    if newly:
        try:
            from juggle_watchdog_poke import poke_watchdog
            poke_watchdog(db.db_path)
        except Exception:
            pass  # 30s backstop covers any poke failure
    return newly


# Reconcile (derive-and-sync) lives in db_topics_reconcile (LOC split,
# 2026-06-13). Re-exported here so existing callers keep importing from
# dbops.db_topics. Imported at module end to avoid a circular import
# (db_topics_reconcile imports get_topic/list_topics/_verified_allowed from here).


def topic_counts(db, project_id) -> dict | None:
    """Display counts over topic nodes (same shape as juggle_graph_status)."""
    from juggle_graph_status import counts_from_states

    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT state FROM nodes "
                "WHERE kind='topic' AND project_id=?",
                (project_id,)
            ).fetchall()
    except Exception:
        return None  # pre-migration DB
    states = [r[0] for r in rows]
    return counts_from_states(states) if states else None


# Back-compat re-exports (LOC split). Placed at module end so db_topics is fully
# defined before these siblings import get_topic/topic_transition/etc. from it
# (avoids a circular-import failure at load time).
from dbops.db_topics_reconcile import (  # noqa: E402,F401
    reconcile_project_topics,
    reconcile_topic_state,
)
from dbops.db_topics_marking import (  # noqa: E402,F401
    mark_topic_completion,
    mark_topic_exec_failed,
    propagate_topic_failure,
)
