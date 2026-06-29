"""Orphan guard: a completed topic must NEVER be silently closed/verified while
its work is unmerged (ahead of main, unintegrated).

Incident (2026-06-17): a send_task false-negative made the watchdog treat a
dispatch as failed, so the topic was never tracked for integrate. When the
coder's complete-agent closed the topic, the work sat committed-in-worktree but
unmerged, and `juggle integrate` reported "Missing worktree fields". G1 already
keeps such a topic out of 'verified'; this adds a detector + flag so the
stranded topic is surfaced (HIGH action item) rather than silently abandoned.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def _repo_with_out_of_band_merge(repo):
    """Build a real git repo where branch ``cyc_X`` was merged into ``main``
    out-of-band (work IS reachable from main), and return that branch name."""
    repo.mkdir(parents=True, exist_ok=True)
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
    return "cyc_X"


def _bind_thread(db, *, repo, branch):
    """Create a thread carrying main_repo_path + worktree_branch; return its id."""
    tid = db.create_thread(topic="orphan-test", session_id="s")
    with db._connect() as c:
        c.execute(
            "UPDATE threads SET main_repo_path=?, worktree_branch=? WHERE id=?",
            (str(repo), branch, tid),
        )
        c.commit()
    return tid


def _bind_node_branch(db, node_id, *, repo, branch):
    """Set nodes.worktree_branch + main_repo_path so orphan_guard can find the repo."""
    with db._connect() as c:
        c.execute(
            "UPDATE nodes SET worktree_branch=?, main_repo_path=? WHERE id=?",
            (branch, str(repo), node_id),
        )
        c.commit()


def _make_db(tmp_path):
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


def _seed_topic(db, topic_id, task_states, *, state="integrating",
                thread_id=None, merged_sha=None):
    from dbops import db_topics, db_graph
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()

    db_topics.create_topic(db, topic_id=topic_id, project_id="INBOX",
                           title=f"Topic {topic_id}")
    with db._connect() as c:
        c.execute(
            "UPDATE graph_topics SET state=?, thread_id=?, merged_sha=? WHERE id=?",
            (state, thread_id, merged_sha, topic_id),
        )
        c.commit()

    # P8: also write the parent node so orphan_guard (which reads nodes) can find it.
    with db._connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO nodes "
            "(id, kind, title, objective, state, project_id, parent_id, "
            "merged_sha, created_at, updated_at) "
            "VALUES (?, 'task', ?, '', ?, 'INBOX', NULL, ?, ?, ?)",
            (topic_id, f"Topic {topic_id}", state, merged_sha, now, now),
        )
        c.commit()

    for i, st in enumerate(task_states):
        tid = f"{topic_id}-t{i}"
        db_graph.create_task(db, task_id=tid, project_id="INBOX", title=tid, prompt="x")
        # create_task dual-writes the child nodes row; bind it to the topic
        # (parent_id) and force the desired state in BOTH stores so orphan_guard
        # (which reads nodes.parent_id/state) sees it (P8 Task 4.1).
        db_graph.set_task_topic(db, tid, topic_id)
        with db._connect() as c:
            c.execute("UPDATE graph_tasks SET state=? WHERE id=?", (st, tid))
            c.execute("UPDATE nodes SET state=? WHERE id=?", (st, tid))
            c.commit()


# --- detector ----------------------------------------------------------------


def test_detector_flags_completed_unmerged_topic(tmp_path):
    """All tasks verified, no merged_sha → completed-but-unmerged orphan."""
    from dbops import orphan_guard

    db = _make_db(tmp_path)
    _seed_topic(db, "T1", ["verified", "verified"], merged_sha=None)
    orphans = orphan_guard.find_unmerged_completed_topics(db)
    assert [t["id"] for t in orphans] == ["T1"]


def test_detector_skips_merged_topic(tmp_path):
    """A topic whose work is merged is not an orphan."""
    from dbops import orphan_guard

    db = _make_db(tmp_path)
    _seed_topic(db, "T1", ["verified"], merged_sha="deadbeef")
    # P8: patch the nodes-based merged check (_node_is_merged)
    with patch("dbops.orphan_guard._node_is_merged", return_value=True):
        orphans = orphan_guard.find_unmerged_completed_topics(db)
    assert orphans == []


def test_detector_skips_topic_with_unfinished_tasks(tmp_path):
    """Work not yet complete (a task still running) → not an orphan."""
    from dbops import orphan_guard

    db = _make_db(tmp_path)
    _seed_topic(db, "T1", ["verified", "running"])
    assert orphan_guard.find_unmerged_completed_topics(db) == []


def test_detector_skips_topic_with_no_tasks(tmp_path):
    from dbops import orphan_guard

    db = _make_db(tmp_path)
    _seed_topic(db, "T1", [])
    assert orphan_guard.find_unmerged_completed_topics(db) == []


def test_detector_skips_already_verified_topic(tmp_path):
    """A proven-merged ('verified') topic is terminal — never re-flagged."""
    from dbops import orphan_guard

    db = _make_db(tmp_path)
    _seed_topic(db, "T1", ["verified"], state="verified", merged_sha="abc")
    assert orphan_guard.find_unmerged_completed_topics(db) == []


# --- flag (surface a blocker) ------------------------------------------------


def test_flag_files_high_action_item_and_dedups(tmp_path):
    from dbops import orphan_guard

    db = _make_db(tmp_path)
    _seed_topic(db, "T1", ["verified"], thread_id="thr-1", merged_sha=None)

    flagged = orphan_guard.flag_unmerged_completed_topics(db)
    assert flagged == ["T1"]
    items = db.get_open_action_items()
    assert any(i["priority"] == "high" and "T1" in i["message"] for i in items)

    # Second pass within the dedup window files nothing new
    flagged2 = orphan_guard.flag_unmerged_completed_topics(db)
    assert flagged2 == []
    assert len(db.get_open_action_items()) == len(items)


# --- core invariant: reconcile never verifies unmerged work ------------------


def test_reconcile_never_marks_unmerged_topic_verified(tmp_path):
    """G1 invariant: all tasks verified but no merged_sha → topic stays
    'integrating' (pre-verified), NEVER 'verified'."""
    # Import via db_topics (fully initialised) to avoid the documented
    # circular-import when importing db_topics_reconcile first.
    from dbops.db_topics import reconcile_topic_state, get_topic

    db = _make_db(tmp_path)
    _seed_topic(db, "T1", ["verified", "verified"], state="integrating",
                merged_sha=None)
    state = reconcile_topic_state(db, "T1")
    assert state == "integrating"
    assert get_topic(db, "T1")["state"] == "integrating"


# --- out-of-band merge: reconcile, don't re-flag ----------------------------


def test_reconcile_stamps_merged_sha_for_out_of_band_merge(tmp_path):
    """Work merged OUTSIDE `juggle integrate` (branch IS an ancestor of main,
    merged_sha never stamped) must be reconciled — stamp merged_sha + verify —
    NOT re-flagged as a stranded orphan."""
    from dbops import orphan_guard
    from dbops.db_topics import get_topic

    repo = tmp_path / "repo"
    branch = _repo_with_out_of_band_merge(repo)

    db = _make_db(tmp_path)
    tid = _bind_thread(db, repo=repo, branch=branch)
    _seed_topic(db, "T1", ["verified"], state="integrating",
                thread_id=tid, merged_sha=None)

    # Before the fix this re-fires every tick; after it, the work is recognised
    # as already-on-main and reconciled.
    reconciled = orphan_guard.reconcile_out_of_band_merges(db)
    assert reconciled == ["T1"]
    stamped = (get_topic(db, "T1")["merged_sha"] or "").strip()
    assert stamped, "merged_sha must be stamped from the merged branch HEAD"
    assert get_topic(db, "T1")["state"] == "verified"

    # No orphan alert is filed (the false-positive loop is closed) and the
    # topic is no longer an orphan.
    assert orphan_guard.find_unmerged_completed_topics(db) == []
    assert orphan_guard.flag_unmerged_completed_topics(db) == []
    assert db.get_open_action_items() == []


def test_flag_auto_reconciles_before_alerting(tmp_path):
    """flag_unmerged_completed_topics reconciles out-of-band merges first, so a
    topic whose work is already on main is verified, not flagged HIGH."""
    from dbops import orphan_guard
    from dbops.db_topics import get_topic

    repo = tmp_path / "repo"
    branch = _repo_with_out_of_band_merge(repo)

    db = _make_db(tmp_path)
    tid = _bind_thread(db, repo=repo, branch=branch)
    _seed_topic(db, "T1", ["verified"], state="integrating",
                thread_id=tid, merged_sha=None)

    assert orphan_guard.flag_unmerged_completed_topics(db) == []
    assert get_topic(db, "T1")["state"] == "verified"
    assert db.get_open_action_items() == []


def test_reconcile_skips_truly_unmerged_branch(tmp_path):
    """A branch NOT reachable from main is a genuine orphan — never reconciled,
    still flagged."""
    from dbops import orphan_guard

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
    _git(repo, "commit", "-m", "work")  # cyc_X ahead of main, NOT merged

    db = _make_db(tmp_path)
    tid = _bind_thread(db, repo=repo, branch="cyc_X")
    _seed_topic(db, "T1", ["verified"], state="integrating",
                thread_id=tid, merged_sha=None)

    assert orphan_guard.reconcile_out_of_band_merges(db) == []
    assert [t["id"] for t in orphan_guard.find_unmerged_completed_topics(db)] == ["T1"]
