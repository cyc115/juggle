"""dbops.state_write — single in-transaction state writer (P8 M3).

``nodes`` is the authoritative state table; the legacy graph_tasks/graph_topics
row is mirrored in LOCKSTEP within the caller's transaction so ``nodes.state`` can
never drift from the legacy row (the M3 post-commit mirror-window bug). The legacy
half is removed in Step 4 when the read sources flip to ``nodes`` and the legacy
tables are dropped.

Both helpers take a caller-supplied connection and DO NOT commit — the caller owns
the transaction (all-or-nothing). They write whichever legacy table holds the id
(a task → graph_tasks, a topic → graph_topics); the non-matching table no-ops.
"""
from __future__ import annotations


def write_state(conn, node_id, new_state, *, now, verified=False, clear_thread=False):
    """Unconditional state write to ``nodes`` AND the legacy row, in lockstep.

    ``verified`` also stamps ``verified_at``; ``clear_thread`` nulls the dispatch
    binding (``nodes.dispatch_thread_id`` / legacy ``thread_id``) — used by the
    'reload' resurrection so a dead thread id is not carried forward.
    """
    # nodes (authoritative) — the agent binding column is dispatch_thread_id.
    nsets, nparams = ["state=?", "updated_at=?"], [new_state, now]
    if verified:
        nsets.append("verified_at=?")
        nparams.append(now)
    if clear_thread:
        nsets.append("dispatch_thread_id=NULL")
    conn.execute(f"UPDATE nodes SET {', '.join(nsets)} WHERE id=?", (*nparams, node_id))
    # legacy mirror (graph_tasks for task-tier, graph_topics for topic-tier).
    lsets, lparams = ["state=?", "updated_at=?"], [new_state, now]
    if verified:
        lsets.append("verified_at=?")
        lparams.append(now)
    if clear_thread:
        lsets.append("thread_id=NULL")
    for tbl in ("graph_tasks", "graph_topics"):
        conn.execute(f"UPDATE {tbl} SET {', '.join(lsets)} WHERE id=?", (*lparams, node_id))


def cas_state(conn, node_id, *, frm, to, now) -> int:
    """Conditional state write (compare-and-swap): ``UPDATE ... WHERE state=:frm``.

    The legacy graph_tasks/graph_topics row is the claim token — it always exists
    for a task/topic, whereas a ``nodes`` row may be absent for directly-created
    rows (e.g. db_graph.create_task in unit tests) until Step 4 makes nodes the
    sole table. Its rowcount is returned as the claim result; ``nodes`` is mirrored
    under the SAME guard so the two stay in lockstep. Returns the legacy rowcount
    (0 = lost race / row not in ``frm``).
    """
    won = 0
    for tbl in ("graph_tasks", "graph_topics"):
        cur = conn.execute(
            f"UPDATE {tbl} SET state=?, updated_at=? WHERE id=? AND state=?",
            (to, now, node_id, frm),
        )
        won += cur.rowcount
    if won:
        conn.execute(
            "UPDATE nodes SET state=?, updated_at=? WHERE id=? AND state=?",
            (to, now, node_id, frm),
        )
    return won
