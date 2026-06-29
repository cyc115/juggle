"""P8 c4-write-cut rollback inverse (R2-3).

After the Step-4 write-cut, ``add_node`` and the task engine write ONLY
``nodes``/``node_edges`` — ``graph_tasks``/``graph_edges`` stop receiving new
rows. The spec's §12.2 "revert the reads, legacy is still populated" rollback is
therefore valid only THROUGH Step 3; from the write-cut onward a bare
``git revert`` of the Step-4 reads restores legacy-*reading* code but leaves the
legacy tables empty for every row created in the write-cut window.

``reverse_backfill_nodes_to_graph`` is the documented clean-path rollback: run it
after reverting the Step-4 reads to project the nodes-only rows back into
``graph_tasks``/``graph_edges`` so the restored legacy engine can see them —
without a full restore from ``juggle.db.bak-pre-p8-step4``.

It is DEAD in the forward path (nothing calls it while nodes is authoritative) and
LIVE only on revert. Idempotent (``INSERT OR IGNORE``); the caller owns the
transaction (no commit here).

R3-1 (corrected vocab): ``graph_tasks.state`` was unified to the node vocab
(``open``/``ready``/``done``/...) in Step 1, so the state is copied DIRECTLY
(identity) — it is NOT routed through ``node_translation.status_for_state`` (which
emits the thread vocab ``active``/``closed`` that the restored legacy engine could
neither read as a ready-set nor transition).
"""
from __future__ import annotations

import sqlite3


def reverse_backfill_nodes_to_graph(conn: sqlite3.Connection) -> None:
    """Reconstruct graph_tasks + graph_edges from the authoritative nodes store.

    For every ``kind='task'`` node, re-INSERT (OR IGNORE) the equivalent
    ``graph_tasks`` row with the node-state value carried over identically
    (R3-1). Re-create ``graph_edges`` from ``node_edges`` for edges whose BOTH
    endpoints are task nodes (so the legacy FK to ``graph_tasks(id)`` holds).
    No-op if the legacy tables are already gone (post terminal-drop).
    """
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "graph_tasks" not in tables or "nodes" not in tables:
        return

    # graph_tasks <- nodes (kind='task'). state copied IDENTITY (R3-1). project_id
    # is NOT NULL in graph_tasks, so fall back to 'INBOX' for an untagged node.
    conn.execute(
        "INSERT OR IGNORE INTO graph_tasks "
        "(id, project_id, title, prompt, verify_cmd, state, thread_id, "
        " handoff, diffstat, verified_at, created_at, updated_at) "
        "SELECT id, COALESCE(project_id, 'INBOX'), title, objective, verify_cmd, "
        "       state, dispatch_thread_id, handoff, diffstat, verified_at, "
        "       created_at, updated_at "
        "FROM nodes WHERE kind='task'"
    )

    if "graph_edges" in tables and "node_edges" in tables:
        # Only edges between two task nodes — graph_edges FKs both ends to
        # graph_tasks(id), which now holds exactly the kind='task' rows above.
        conn.execute(
            "INSERT OR IGNORE INTO graph_edges (task_id, depends_on_id) "
            "SELECT e.node_id, e.depends_on_id FROM node_edges e "
            "WHERE e.node_id IN (SELECT id FROM nodes WHERE kind='task') "
            "  AND e.depends_on_id IN (SELECT id FROM nodes WHERE kind='task')"
        )
