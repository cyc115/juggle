"""Regression pin: loop --purge full cascade + id-reuse collision.

Incident 2026-07-07 (loop --purge orphaned nodes + id-reuse collision):
`schedule delete <Lid> --purge` deleted ONLY the `loops` row and left orphans —
the loop's graph nodes (topic + every run-seq task generation + the loop
project's conversation node, all carrying project_id=<the loop's project>),
their `node_edges`, the `projects` row (soft-closed, NOT removed), and the
loop-owned `agent_runs` ledger rows. The freed L-id was then REUSED on the next
`loop create`, whose fresh nodes collided with the survivors
(`sqlite3.IntegrityError: UNIQUE constraint failed: nodes.id`), rolling back the
whole atomic create — a same-shaped loop was un-recreatable after a purge.

Purge must fully cascade-delete EVERYTHING the loop owns, atomically, so id-reuse
is collision-free.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from juggle_cmd_loop_create import create_loop_atomic  # noqa: E402
from juggle_cmd_schedule import delete_schedule  # noqa: E402
from dbops.schema import _now  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "purge.db"))
    d.init_db()
    return d


def _template(ntasks: int = 2) -> dict:
    tasks = [
        {"id": f"t{i}", "title": f"task {i}", "prompt": f"do {i}",
         "role": "coder", "model": "sonnet", "verify_cmd": None, "deps": []}
        for i in range(ntasks)
    ]
    return {"topics": [{
        "id": "digest", "title": "Digest", "objective": "obj",
        "delivery": "merge", "tasks": tasks,
    }]}


def _seed_fired_state(db: JuggleDB, project_id: str, topic_id: str, task_id: str) -> str:
    """Simulate a loop that has FIRED: a conversation node under the loop project,
    a dispatch edge into it, a pool agent bound to it, and a loop-owned agent_runs
    ledger row — exactly the orphans the buggy purge left behind."""
    now = _now()
    conv_id = f"{project_id}-conv"
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO nodes (id, kind, title, objective, state, project_id, "
            "created_at, updated_at) VALUES (?, 'conversation', 'loop convo', '', "
            "'open', ?, ?, ?)",
            (conv_id, project_id, now, now),
        )
        conn.execute(
            "INSERT INTO node_edges (node_id, depends_on_id, kind) "
            "VALUES (?, ?, 'dispatch')",
            (topic_id, conv_id),
        )
        conn.execute(
            "INSERT INTO agents (id, role, pane_id, assigned_thread, status, "
            "context_threads, created_at, last_active) "
            "VALUES ('ag-loop', 'coder', 'pane1', ?, 'busy', '[]', ?, ?)",
            (conv_id, now, now),
        )
        conn.execute(
            "INSERT INTO agent_runs (thread_id, project_id, topic_id, task_id, "
            "input_prompt, status, dispatched_at) "
            "VALUES (?, ?, ?, ?, 'prompt', 'completed', ?)",
            (conv_id, project_id, topic_id, task_id, now),
        )
        conn.commit()
    return conv_id


def test_purge_full_cascade_and_id_reuse(db):
    """2026-07-07 loop --purge orphaned nodes + id-reuse collision: --purge must
    cascade-delete every node/edge/ledger row the loop owns AND drop the project
    row, so recreating the SAME template (which reuses the freed L-id) does not
    collide on nodes.id."""
    r = create_loop_atomic(db, template=_template(ntasks=2), cadence="every 1h")
    loop_id, project_id, topic_id = r["loop_id"], r["project_id"], r["topic_id"]
    task_ids = [n for n in r["node_ids"] if n != topic_id]
    conv_id = _seed_fired_state(db, project_id, topic_id, task_ids[0])

    # Pre-purge sanity: the loop owns nodes.
    with db._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE project_id=?", (project_id,)
        ).fetchone()[0] > 0

    res = delete_schedule(db, loop_id, purge=True)
    assert res["type"] == "loop" and res["action"] == "purge"

    with db._connect() as conn:
        # (a) zero nodes remain for the loop's project (topic + tasks + conversation)
        assert conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE project_id=?", (project_id,)
        ).fetchone()[0] == 0
        # (b) the project row is HARD-removed, not just closed
        assert conn.execute(
            "SELECT COUNT(*) FROM projects WHERE id=?", (project_id,)
        ).fetchone()[0] == 0
        # the loops row is gone
        assert conn.execute(
            "SELECT COUNT(*) FROM loops WHERE id=?", (loop_id,)
        ).fetchone()[0] == 0
        # (c) no dangling node_edges referencing the removed nodes
        assert conn.execute(
            "SELECT COUNT(*) FROM node_edges WHERE node_id=? OR depends_on_id=?",
            (topic_id, conv_id),
        ).fetchone()[0] == 0
        # (c) no dangling loop-owned agent_runs (by project OR topic/task)
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_runs WHERE project_id=? OR topic_id=? "
            "OR task_id=?", (project_id, topic_id, task_ids[0])
        ).fetchone()[0] == 0
        # (c) no pool agent dangles onto a removed (soon-reused) node id
        assert conn.execute(
            "SELECT COUNT(*) FROM agents WHERE assigned_thread=?", (conv_id,)
        ).fetchone()[0] == 0

    # (d) recreating the SAME template SUCCEEDS — the freed L-id is reused with no
    # nodes.id collision (the whole point: a purged loop is fully recreatable).
    r2 = create_loop_atomic(db, template=_template(ntasks=2), cadence="every 1h")
    assert r2["loop_id"] == loop_id        # freed L-id reused
    assert r2["project_id"] == project_id  # freed P-id reused
    with db._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE project_id=?", (project_id,)
        ).fetchone()[0] > 0
