"""P8 read-collapse regression pins.

RED before P8 implementation (readers still use graph_tasks / graph_topics).
GREEN after.

2026-06-20 incident: cockpit_model/graph_dag/orphan_guard all read from legacy
tables; P8 migrates them to the unified nodes table.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _make_db(tmp_path):
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


def _insert_task_node(conn, *, node_id, project_id, state, parent_id=None,
                      kind="task", merged_sha=None, worktree_branch=None,
                      main_repo_path=None):
    """Insert directly into nodes (no legacy dual-write). ``kind='topic'`` seeds a
    topic-tier root node (P8 M2 discriminator); default 'task' seeds a task."""
    conn.execute(
        "INSERT INTO nodes "
        "(id, kind, title, objective, state, project_id, parent_id, "
        "merged_sha, worktree_branch, main_repo_path, created_at, updated_at) "
        "VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        (node_id, kind, f"Task {node_id}", state, project_id, parent_id,
         merged_sha, worktree_branch, main_repo_path),
    )


def _ensure_project(conn, pid):
    conn.execute(
        "INSERT OR IGNORE INTO projects "
        "(id, name, objective, success_criteria, out_of_scope, status, "
        "created_at, last_active) VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (pid, pid, "", "[]", "", "active"),
    )


# ---------------------------------------------------------------------------
# cockpit_model.snapshot reads from nodes (not graph_tasks)
# ---------------------------------------------------------------------------


def test_cockpit_snapshot_graph_by_project_reads_nodes(tmp_path):
    """P8-pin: graph_by_project derived from nodes, not graph_tasks.

    2026-06-20: cockpit migrated from graph_tasks read to nodes read.
    """
    db = _make_db(tmp_path)
    with db._connect() as conn:
        _ensure_project(conn, "P1")
        _insert_task_node(conn, node_id="N1", project_id="P1", state="running")
        conn.commit()
    # graph_tasks is EMPTY — graph_by_project must still be populated from nodes
    from juggle_cockpit_model import snapshot

    state = snapshot(db)
    assert state.graph_by_project is not None, (
        "graph_by_project must come from nodes (graph_tasks is empty)"
    )
    assert "P1" in state.graph_by_project


def test_cockpit_snapshot_task_state_none(tmp_path):
    """P8-pin: task_state is None — no graph_tasks.thread_id join in P8.

    2026-06-20: task_state_by_thread dict removed; nodes has no thread_id.
    """
    db = _make_db(tmp_path)
    tid = db.create_thread("conv", session_id="s")
    with db._connect() as conn:
        conn.execute(
            "UPDATE threads SET status='active', "
            "last_active_at=datetime('now') WHERE id=?",
            (tid,),
        )
        conn.commit()
    from juggle_cockpit_model import snapshot

    state = snapshot(db)
    topic = next((t for t in state.topics if t.id == tid), None)
    assert topic is not None
    # In P8 task_state is never populated from graph_tasks.thread_id
    assert topic.task_state is None


# ---------------------------------------------------------------------------
# cockpit_graph_dag reads from nodes (not graph_topics)
# ---------------------------------------------------------------------------


def test_graph_dag_reads_nodes_not_graph_topics(tmp_path):
    """P8-pin: DAG built from nodes (topic nodes), not graph_topics.

    2026-06-20: cockpit_graph_dag migrated to nodes primary source.
    2026-06-29 (P8 M2): topic roots are now kind='topic'.
    """
    db = _make_db(tmp_path)
    with db._connect() as conn:
        _ensure_project(conn, "P1")
        _insert_task_node(conn, node_id="TASK1", project_id="P1", state="ready",
                          kind="topic")
        conn.commit()
    # graph_topics is EMPTY — DAG must find TASK1 from nodes
    from juggle_cockpit_graph_dag import load_graph_dags

    with db._connect() as conn:
        dags = load_graph_dags(conn)
    assert len(dags) >= 1, "DAG must be built from nodes even with empty graph_topics"
    task_ids = [t.id for t in dags[0].tasks]
    assert "TASK1" in task_ids


def test_graph_dag_member_tasks_from_nodes_children(tmp_path):
    """P8-pin: member_tasks come from nodes WHERE parent_id=?, not graph_tasks.

    2026-06-20: _load_one reads children from nodes.parent_id.
    """
    db = _make_db(tmp_path)
    with db._connect() as conn:
        _ensure_project(conn, "P1")
        _insert_task_node(conn, node_id="PAR1", project_id="P1", state="running",
                          kind="topic")
        _insert_task_node(conn, node_id="CHI1", project_id="P1", state="verified",
                          parent_id="PAR1")
        conn.commit()
    from juggle_cockpit_graph_dag import load_graph_dags

    with db._connect() as conn:
        dags = load_graph_dags(conn)
    assert dags, "DAG must be built from nodes"
    member_tasks = dags[0].member_tasks or {}
    assert "PAR1" in member_tasks, "parent node must be DAG vertex"
    child_ids = [c["id"] for c in member_tasks["PAR1"]]
    assert "CHI1" in child_ids, "child node must appear in member_tasks"


# ---------------------------------------------------------------------------
# orphan_guard reads from nodes (not graph_topics / graph_tasks)
# ---------------------------------------------------------------------------


def _seed_node_topic(db, node_id, child_states, *, project_id="INBOX",
                     state="running", merged_sha=None, worktree_branch=None,
                     main_repo_path=None):
    """Seed a topic node + children directly into nodes (no graph_topics write)."""
    with db._connect() as conn:
        _insert_task_node(
            conn, node_id=node_id, project_id=project_id, state=state,
            kind="topic", merged_sha=merged_sha, worktree_branch=worktree_branch,
            main_repo_path=main_repo_path,
        )
        for i, st in enumerate(child_states):
            _insert_task_node(
                conn, node_id=f"{node_id}-c{i}", project_id=project_id,
                state=st, parent_id=node_id,
            )
        conn.commit()


def test_orphan_guard_find_reads_nodes(tmp_path):
    """P8-pin: find_unmerged_completed_topics reads from nodes, not graph_topics.

    2026-06-20: orphan_guard migrated to nodes primary source.
    """
    from dbops import orphan_guard

    db = _make_db(tmp_path)
    _seed_node_topic(db, "N1", child_states=["verified", "verified"],
                     merged_sha=None)
    orphans = orphan_guard.find_unmerged_completed_topics(db)
    assert [o["id"] for o in orphans] == ["N1"], (
        "orphan_guard must detect orphans from nodes table"
    )


def test_orphan_guard_skips_node_with_unverified_child(tmp_path):
    """P8-pin: node with an unfinished child is not an orphan."""
    from dbops import orphan_guard

    db = _make_db(tmp_path)
    _seed_node_topic(db, "N1", child_states=["verified", "running"])
    assert orphan_guard.find_unmerged_completed_topics(db) == []


def test_orphan_guard_skips_node_with_no_children(tmp_path):
    """P8-pin: node with no children is not an orphan."""
    from dbops import orphan_guard

    db = _make_db(tmp_path)
    _seed_node_topic(db, "N1", child_states=[])
    assert orphan_guard.find_unmerged_completed_topics(db) == []


def test_orphan_guard_skips_node_with_merged_sha_on_main(tmp_path):
    """P8-pin: node with merged_sha already on main is not an orphan (G1 gate)."""
    from dbops import orphan_guard
    from unittest.mock import patch

    db = _make_db(tmp_path)
    _seed_node_topic(db, "N1", child_states=["verified"], merged_sha="abc123")
    # Patch _node_is_merged to simulate the node being merged
    with patch("dbops.orphan_guard._node_is_merged", return_value=True):
        orphans = orphan_guard.find_unmerged_completed_topics(db)
    assert orphans == []


def test_orphan_guard_reconcile_stamps_nodes_merged_sha(tmp_path):
    """P8-pin: reconcile_out_of_band_merges stamps nodes.merged_sha.

    2026-06-20: reconcile now writes to both nodes AND (compat) graph_topics.
    """
    from dbops import orphan_guard

    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args):
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    (repo / "f.txt").write_text("base")
    _git("add", ".")
    _git("commit", "-m", "base")
    _git("checkout", "-b", "cyc_R")
    (repo / "f.txt").write_text("work")
    _git("add", ".")
    _git("commit", "-m", "work")
    _git("checkout", "main")
    _git("merge", "--no-ff", "cyc_R", "-m", "merge cyc_R")

    db = _make_db(tmp_path)
    _seed_node_topic(
        db, "N1", child_states=["verified"],
        merged_sha=None,
        worktree_branch="cyc_R",
        main_repo_path=str(repo),
    )

    reconciled = orphan_guard.reconcile_out_of_band_merges(db)
    assert reconciled == ["N1"]

    with db._connect() as conn:
        row = conn.execute(
            "SELECT merged_sha, state FROM nodes WHERE id='N1'"
        ).fetchone()
    assert row[0], "nodes.merged_sha must be stamped by reconcile"
    assert row[1] == "verified", "nodes.state must be 'verified' after reconcile"
    # Must no longer be an orphan
    assert orphan_guard.find_unmerged_completed_topics(db) == []
