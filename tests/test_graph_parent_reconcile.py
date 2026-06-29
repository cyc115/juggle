"""Regression pin — DEFECT #4907 (2026-06-21): graph-load dropped
nodes.parent_id → a verified topic looked CHILDLESS to
find_unmerged_completed_topics() → reconcile_out_of_band_merges() never stamped
merged_sha → the watchdog re-dispatched the already-completed step in a loop.

The parent_id dual-write (db_graph.set_task_topic) only landed in the C2 read
flip; pre-flip task nodes carry NULL parent_id while legacy graph_tasks.topic_id
is correct, and some task-node states drifted (nodes 'ready' vs legacy
'verified'). These pins exercise the repair: reconcile re-links parent_id from
graph_tasks.topic_id and resyncs task-node state from the legacy authoritative
value, restoring orphan detection and breaking the re-dispatch loop.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _make_db(tmp_path):
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


def _seed_topic(db, topic_id, task_states, *, state="integrating", merged_sha=None):
    """Build a kind='topic' node + child task nodes, then DROP nodes.parent_id and
    drift nodes.state to reproduce the defect, leaving graph_tasks authoritative."""
    from datetime import datetime, timezone

    from dbops import db_graph, db_topics

    now = datetime.now(timezone.utc).isoformat()
    db_topics.create_topic(db, topic_id=topic_id, project_id="INBOX",
                           title=f"Topic {topic_id}")
    with db._connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO nodes "
            "(id, kind, title, objective, state, project_id, parent_id, "
            "merged_sha, created_at, updated_at) "
            "VALUES (?, 'topic', ?, '', ?, 'INBOX', NULL, ?, ?, ?)",
            (topic_id, f"Topic {topic_id}", state, merged_sha, now, now),
        )
        c.execute(
            "UPDATE nodes SET state=?, merged_sha=? WHERE id=? AND kind='topic'",
            (state, merged_sha, topic_id),
        )
        c.commit()
    for i, st in enumerate(task_states):
        tid = f"{topic_id}-t{i}"
        db_graph.create_task(db, task_id=tid, project_id="INBOX", title=tid, prompt="x")
        db_graph.set_task_topic(db, tid, topic_id)
        with db._connect() as c:
            c.execute("UPDATE graph_tasks SET state=? WHERE id=?", (st, tid))
            c.commit()


def _break_nodes(db, drift_state="ready"):
    """Reproduce the defect: null every task node's parent_id and drift its state
    away from the legacy graph_tasks value."""
    with db._connect() as c:
        c.execute("UPDATE nodes SET parent_id=NULL, state=? WHERE kind='task'",
                  (drift_state,))
        c.commit()


def test_reconcile_relinks_dropped_parent_id_and_resyncs_state(tmp_path):
    """RED before fix: childless topic invisible to the orphan detector. After
    reconcile: parent_id re-linked from graph_tasks.topic_id, state resynced from
    legacy, and the topic's children are found again."""
    from dbops import orphan_guard
    from dbops.migration_parent_relink import reconcile_node_parentage

    db = _make_db(tmp_path)
    _seed_topic(db, "T1", ["verified", "verified"], state="integrating")
    _break_nodes(db)

    # Defect symptom: topic looks childless → not detected.
    assert orphan_guard.find_unmerged_completed_topics(db) == []

    counts = reconcile_node_parentage(db)
    assert counts["parent_relinked"] == 2
    assert counts["state_resynced"] == 2

    # Repaired: children re-linked + verified → topic surfaces as completed-unmerged.
    assert [t["id"] for t in orphan_guard.find_unmerged_completed_topics(db)] == ["T1"]

    # Idempotent: a second pass changes nothing.
    again = reconcile_node_parentage(db)
    assert again == {"parent_relinked": 0, "state_resynced": 0}


def test_out_of_band_reconcile_selfheals_dropped_parent_id(tmp_path):
    """The watchdog reconcile path self-heals dropped parent_id BEFORE detection,
    so an out-of-band-merged topic is stamped (loop broken), not re-dispatched."""
    from dbops import orphan_guard
    from dbops.db_topics import get_topic

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "cyc_X")
    (repo / "f.txt").write_text("work")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "work")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "cyc_X", "-m", "merge cyc_X")  # out-of-band

    db = _make_db(tmp_path)
    tid = db.create_thread(topic="orphan-test", session_id="s")
    db.update_thread(tid, main_repo_path=str(repo), worktree_branch="cyc_X")
    _seed_topic(db, "T1", ["verified"], state="integrating")
    # Bind the topic node's dispatch thread so orphan_guard resolves repo/branch.
    from dbops.db_topics import set_topic_thread
    set_topic_thread(db, "T1", tid)
    _break_nodes(db)

    reconciled = orphan_guard.reconcile_out_of_band_merges(db)
    assert reconciled == ["T1"]
    assert (get_topic(db, "T1")["merged_sha"] or "").strip()
