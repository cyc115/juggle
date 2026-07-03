"""dbops.db_graph_setters — single-column task-node write helpers.

Extracted from db_graph (LOC gate) — these are simple, independent
`UPDATE nodes SET <col>=? ... WHERE id=? AND kind='task'` writers with no
state-machine involvement (task_transition remains the sole state writer).
Re-exported from db_graph for the existing import surface.
"""

from __future__ import annotations

from dbops.dispatch_edge import bind_dispatch_thread
from dbops.schema import _now


def set_task_thread(db, task_id: str, thread_id) -> None:
    """Bind the dispatch thread as a typed kind='dispatch' node_edge (P8 M1/Q2;
    thread_id=None unbinds). The legacy graph_tasks.thread_id write was cut
    (P8 c4-write-cut)."""
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET updated_at=? WHERE id=? AND kind='task'",
            (now, task_id),
        )
        bind_dispatch_thread(conn, task_id, thread_id)
        conn.commit()


def set_task_handoff(db, task_id: str, handoff: str) -> None:
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET handoff=?, updated_at=? WHERE id=? AND kind='task'",
            (handoff, now, task_id),
        )
        conn.commit()


def bump_verify_retry(db, task_id: str, failure: str | None) -> None:
    """Verify-fallback: increment the bounded-retry counter and store the prior
    verify_cmd failure output (for fresh-re-dispatch prompt injection). Never
    writes state — the caller resets the task via task_transition."""
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET verify_retries = COALESCE(verify_retries, 0) + 1, "
            "verify_failure=?, updated_at=? WHERE id=? AND kind='task'",
            (failure, now, task_id),
        )
        conn.commit()


def set_task_fail_envelope(db, task_id: str, fail_envelope: str) -> None:
    """Persist the JSON fail envelope for the most recent integrate refusal."""
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET fail_envelope=?, updated_at=? WHERE id=? AND kind='task'",
            (fail_envelope, now, task_id),
        )
        conn.commit()


def set_task_diffstat(db, task_id: str, diffstat: str) -> None:
    """Pre-merge diffstat captured by integrate (hydration enrichment)."""
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET diffstat=?, updated_at=? WHERE id=? AND kind='task'",
            (diffstat, now, task_id),
        )
        conn.commit()


def set_task_topic(db, task_id: str, topic_id, conn=None) -> None:
    """Assign a task to its topic — writes the authoritative nodes.parent_id
    (get_task maps parent_id→topic_id). The legacy graph_tasks.topic_id write was
    cut (P8 c4-write-cut)."""
    from dbops.db_graph import _cx

    with _cx(db, conn) as c:
        c.execute(
            "UPDATE nodes SET parent_id=? WHERE id=? AND kind='task'",
            (topic_id, task_id),
        )
