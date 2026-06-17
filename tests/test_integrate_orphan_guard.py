"""Orphan guard: a completed topic must NEVER be silently closed/verified while
its work is unmerged (ahead of main, unintegrated).

Incident (2026-06-17): a send_task false-negative made the watchdog treat a
dispatch as failed, so the topic was never tracked for integrate. When the
coder's complete-agent closed the topic, the work sat committed-in-worktree but
unmerged, and `juggle integrate` reported "Missing worktree fields". G1 already
keeps such a topic out of 'verified'; this adds a detector + flag so the
stranded topic is surfaced (HIGH action item) rather than silently abandoned.
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _make_db(tmp_path):
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


def _seed_topic(db, topic_id, task_states, *, state="integrating",
                thread_id=None, merged_sha=None):
    from dbops import db_topics, db_graph

    db_topics.create_topic(db, topic_id=topic_id, project_id="INBOX",
                           title=f"Topic {topic_id}")
    with db._connect() as c:
        c.execute(
            "UPDATE graph_topics SET state=?, thread_id=?, merged_sha=? WHERE id=?",
            (state, thread_id, merged_sha, topic_id),
        )
        c.commit()
    for i, st in enumerate(task_states):
        tid = f"{topic_id}-t{i}"
        db_graph.create_task(db, task_id=tid, project_id="INBOX", title=tid, prompt="x")
        with db._connect() as c:
            c.execute("UPDATE graph_tasks SET topic_id=?, state=? WHERE id=?",
                      (topic_id, st, tid))
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
    with patch("dbops.orphan_guard.topic_is_merged", return_value=True):
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
