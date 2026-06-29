"""Regression pin — DEFECT #4907 (2026-06-21): a verified topic whose child task
nodes had NULL nodes.parent_id looked CHILDLESS to find_unmerged_completed_topics()
→ reconcile_out_of_band_merges() never stamped merged_sha → the watchdog
re-dispatched the already-completed step in a loop.

P8 terminal drop (2026-06-29): nodes is the SOLE store and the legacy graph_tasks
table is DROPPED (Migration 55). The original #4907 drift — a NULL parent_id while
graph_tasks.topic_id was correct — can no longer occur: db_graph.set_task_topic
writes nodes.parent_id directly, and there is no legacy table to drift from. These
pins therefore assert the surviving behavioral guarantee through the new seam:
the parent linkage written to nodes makes a completed-but-unmerged topic visible
to the orphan detector (no re-dispatch loop), and the residual parent-relink
reconcile is a drop-safe no-op that never clobbers a live node.
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
    """Seed a kind='topic' node + child task nodes via the production API. P8
    terminal: set_task_topic writes nodes.parent_id directly (the sole store) so the
    children are correctly linked — no legacy graph_tasks snapshot exists."""
    from dbops import db_graph, db_topics

    db_topics.create_topic(db, topic_id=topic_id, project_id="INBOX",
                           title=f"Topic {topic_id}")
    with db._connect() as c:
        c.execute(
            "UPDATE nodes SET state=?, merged_sha=? WHERE id=? AND kind='topic'",
            (state, merged_sha, topic_id),
        )
        c.commit()
    for i, st in enumerate(task_states):
        tid = f"{topic_id}-t{i}"
        db_graph.create_task(db, task_id=tid, project_id="INBOX", title=tid, prompt="x")
        db_graph.set_task_topic(db, tid, topic_id)  # writes nodes.parent_id
        with db._connect() as c:
            c.execute("UPDATE nodes SET state=? WHERE id=? AND kind='task'", (st, tid))
            c.commit()


def test_completed_topic_detected_via_nodes_parent_link(tmp_path):
    """The #4907 anti-loop guarantee through the new seam: a topic whose children
    are ALL verified but whose work is unmerged is detected as a completed-but-
    unmerged orphan — because set_task_topic linked the children via nodes.parent_id
    (the sole store). A NULL-parent childless topic would be missed → re-dispatch loop."""
    from dbops import orphan_guard

    db = _make_db(tmp_path)
    _seed_topic(db, "T1", ["verified", "verified"], state="integrating")
    assert [t["id"] for t in orphan_guard.find_unmerged_completed_topics(db)] == ["T1"]


def test_parent_relink_is_drop_safe_noop(tmp_path):
    """P8 terminal: graph_tasks is dropped, so the residual parent-relink reconcile
    has no legacy source — it must be a no-op and NEVER clobber a live node's state
    or parent_id (RED on the pre-write-cut reconcile, which overwrote both)."""
    from dbops.migration_parent_relink import reconcile_node_parentage

    db = _make_db(tmp_path)
    _seed_topic(db, "T1", ["verified"], state="integrating")
    with db._connect() as c:
        c.execute("UPDATE nodes SET state='done' WHERE id='T1-t0' AND kind='task'")
        c.commit()
    assert reconcile_node_parentage(db) == {"parent_relinked": 0, "state_resynced": 0}
    with db._connect() as c:
        row = c.execute("SELECT state, parent_id FROM nodes WHERE id='T1-t0'").fetchone()
    assert row[0] == "done"      # live state untouched
    assert row[1] == "T1"        # parent linkage untouched


def test_out_of_band_merge_stamps_merged_sha(tmp_path):
    """reconcile_out_of_band_merges stamps merged_sha for a completed topic whose
    work is already on main (loop broken), now that the children are linked via
    nodes.parent_id alone."""
    from dbops import orphan_guard
    from dbops.db_topics import get_topic, set_topic_thread

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
    set_topic_thread(db, "T1", tid)

    assert orphan_guard.reconcile_out_of_band_merges(db) == ["T1"]
    assert (get_topic(db, "T1")["merged_sha"] or "").strip()
