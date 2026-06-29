"""dbops.state_write — single in-transaction state writer (P8 M3).

``nodes`` is the SOLE authoritative state table (P8 c4-write-cut: the legacy
graph_tasks/graph_topics mirror was removed — those tables stop receiving writes).

Both helpers take a caller-supplied connection and DO NOT commit — the caller owns
the transaction (all-or-nothing).
"""
from __future__ import annotations


def write_state(conn, node_id, new_state, *, now, verified=False, clear_thread=False):
    """Unconditional state write to the authoritative ``nodes`` row.

    ``verified`` also stamps ``verified_at``; ``clear_thread`` drops the dispatch
    binding (the typed kind='dispatch' node_edge) — used by the 'reload'
    resurrection so a dead thread id is not carried forward.
    """
    nsets, nparams = ["state=?", "updated_at=?"], [new_state, now]
    if verified:
        nsets.append("verified_at=?")
        nparams.append(now)
    conn.execute(f"UPDATE nodes SET {', '.join(nsets)} WHERE id=?", (*nparams, node_id))
    if clear_thread:
        from dbops.dispatch_edge import clear_dispatch_thread
        clear_dispatch_thread(conn, node_id)


def cas_state(conn, node_id, *, frm, to, now) -> int:
    """Conditional state write (compare-and-swap): ``UPDATE ... WHERE state=:frm``.

    ``nodes`` is the authoritative claim token (P8 Task 4.1) — the task/topic
    readers compute the ready set from it. Returns the nodes rowcount (0 = lost
    race / row not in ``frm``). The legacy graph_tasks/graph_topics mirror was
    removed in the c4-write-cut.
    """
    cur = conn.execute(
        "UPDATE nodes SET state=?, updated_at=? WHERE id=? AND state=?",
        (to, now, node_id, frm),
    )
    return cur.rowcount
