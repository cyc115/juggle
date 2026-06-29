"""P8 c4-write-cut rollback inverse (R2-3 + R3-1).

After the Step-4 write-cut, task rows live ONLY in nodes/node_edges. If the
Step-4 reads are reverted, the restored legacy engine reads graph_tasks/graph_edges
— which are now empty. ``reverse_backfill_nodes_to_graph`` reconstructs them from
nodes so a revert can see the nodes-only rows WITHOUT a full DB restore.

R3-1 (corrected vocab): graph_tasks.state was unified to the node vocab
(open/ready/done/...) in Step 1, so the reconstruction copies nodes.state
DIRECTLY (identity) — it must NOT route through node_translation.status_for_state
(which emits the thread vocab active/closed the restored legacy engine cannot
transition).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from juggle_db import JuggleDB


@pytest.fixture
def tmp_db(tmp_path: Path) -> JuggleDB:
    db = JuggleDB(db_path=str(tmp_path / "rb.db"))
    db.init_db()
    return db


def _drive_to_done(db, node_id: str) -> None:
    from dbops import db_graph
    for event in ("claim", "dispatch", "integrate_start", "integrate_ok", "g1_pass"):
        db_graph.task_transition(db, node_id, event)


def test_reverse_backfill_reconstructs_graph_tasks(tmp_db):
    """2026-06-29 P8 R2-3: after the write-cut, a nodes-only task must be
    reconstructable into graph_tasks so a revert of legacy-reading code sees it."""
    from juggle_add_node import add_node
    from dbops.p8_reverse_backfill import reverse_backfill_nodes_to_graph
    r = add_node(tmp_db, kind="task", title="x", project_id="INBOX")  # nodes-only
    with tmp_db._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM graph_tasks WHERE id=?", (r["node_id"],)
        ).fetchone()[0] == 0
        reverse_backfill_nodes_to_graph(conn)
        conn.commit()
        row = conn.execute(
            "SELECT state FROM graph_tasks WHERE id=?", (r["node_id"],)
        ).fetchone()
    assert row is not None  # legacy engine can now see the task


def test_reverse_backfill_state_is_node_vocab_not_thread_vocab(tmp_db):
    """2026-06-29 P8 R3-1: the reconstructed graph_tasks.state equals the node-state
    value ('done'), NOT the thread-vocab 'closed' that status_for_state would emit."""
    from juggle_add_node import add_node
    from dbops.p8_reverse_backfill import reverse_backfill_nodes_to_graph
    r = add_node(tmp_db, kind="task", title="x", project_id="INBOX")
    _drive_to_done(tmp_db, r["node_id"])
    with tmp_db._connect() as conn:
        # node carries the unified vocab value:
        assert conn.execute(
            "SELECT state FROM nodes WHERE id=?", (r["node_id"],)
        ).fetchone()[0] == "done"
        reverse_backfill_nodes_to_graph(conn)
        conn.commit()
        state = conn.execute(
            "SELECT state FROM graph_tasks WHERE id=?", (r["node_id"],)
        ).fetchone()[0]
    assert state == "done", "R3-1: must copy node state identity, not status_for_state"
    assert state != "closed"


def test_reverse_backfill_recreates_graph_edges(tmp_db):
    """2026-06-29 P8 R2-3: dependency edges are reconstructed from node_edges so the
    restored legacy ready-set query sees the same DAG."""
    from juggle_add_node import add_node
    from dbops.p8_reverse_backfill import reverse_backfill_nodes_to_graph
    dep = add_node(tmp_db, kind="task", title="dep", project_id="INBOX")
    child = add_node(tmp_db, kind="task", title="child", project_id="INBOX",
                     deps=[dep["node_id"]])
    with tmp_db._connect() as conn:
        reverse_backfill_nodes_to_graph(conn)
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE task_id=? AND depends_on_id=?",
            (child["node_id"], dep["node_id"]),
        ).fetchone()[0]
    assert n == 1


def test_reverse_backfill_idempotent(tmp_db):
    """2026-06-29 P8 R2-3: a second run is a no-op (INSERT OR IGNORE) — no
    duplicate rows, no error."""
    from juggle_add_node import add_node
    from dbops.p8_reverse_backfill import reverse_backfill_nodes_to_graph
    r = add_node(tmp_db, kind="task", title="x", project_id="INBOX")
    with tmp_db._connect() as conn:
        reverse_backfill_nodes_to_graph(conn)
        reverse_backfill_nodes_to_graph(conn)
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM graph_tasks WHERE id=?", (r["node_id"],)
        ).fetchone()[0]
    assert n == 1
