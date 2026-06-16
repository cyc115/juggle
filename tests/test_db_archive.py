"""JuggleDB tests: archive_thread, get_archive_candidates, unarchive_thread (split from test_juggle_db.py, 2026-06-10)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_context import get_thread_state
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d

# ------------------------------------------------------------------
# archive_thread tests
# ------------------------------------------------------------------


def test_archive_thread_sets_status_and_show_in_list(db):
    """archive_thread sets status='archived' and show_in_list=0."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.archive_thread(tid)
    t = db.get_thread(tid)
    assert t is not None
    assert t["status"] == "archived"
    assert t["show_in_list"] == 0


def test_archive_thread_does_not_delete(db):
    """archive_thread does not delete the thread row."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.archive_thread(tid)
    assert db.get_thread(tid) is not None


def test_show_in_list_defaults_to_1(db):
    """New threads have show_in_list=1 by default."""
    tid = db.create_thread("Topic A", session_id="s1")
    t = db.get_thread(tid)
    assert t is not None
    assert t["show_in_list"] == 1


# ------------------------------------------------------------------
# get_archive_candidates tests
# ------------------------------------------------------------------


def test_get_archive_candidates_empty(db):
    """No candidates when only one active thread exists."""
    tid_a = db.create_thread("Topic A", session_id="s1")
    db.set_current_thread(tid_a)
    candidates = db.get_archive_candidates()
    assert candidates == []


def test_get_archive_candidates_done(db):
    """A done thread (non-current) is a candidate."""
    tid_a = db.create_thread("Topic A", session_id="s1")
    tid_b = db.create_thread("Topic B", session_id="s1")
    db.set_current_thread(tid_a)
    db.update_thread(tid_b, status="done")
    candidates = db.get_archive_candidates()
    assert len(candidates) == 1
    assert candidates[0]["id"] == tid_b


def test_get_archive_candidates_failed(db):
    """A failed thread (non-current) is a candidate."""
    tid_a = db.create_thread("Topic A", session_id="s1")
    tid_b = db.create_thread("Topic B", session_id="s1")
    db.set_current_thread(tid_a)
    db.update_thread(tid_b, status="failed")
    candidates = db.get_archive_candidates()
    assert len(candidates) == 1
    assert candidates[0]["id"] == tid_b


def test_get_archive_candidates_old_inactive(db):
    """A thread inactive > 48 hours (not background/waiting) is a candidate."""
    from datetime import datetime, timezone, timedelta

    tid_a = db.create_thread("Topic A", session_id="s1")
    tid_b = db.create_thread("Topic B", session_id="s1")
    db.set_current_thread(tid_a)
    old_time = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    db.update_thread(tid_b, last_active=old_time, status="active")
    candidates = db.get_archive_candidates()
    assert any(c["id"] == tid_b for c in candidates)


def test_get_archive_candidates_excludes_current(db):
    """Current thread is never a candidate even if it would otherwise qualify."""
    tid_a = db.create_thread("Topic A", session_id="s1")
    db.set_current_thread(tid_a)
    db.update_thread(tid_a, status="done")
    candidates = db.get_archive_candidates()
    assert all(c["id"] != tid_a for c in candidates)


def test_get_archive_candidates_excludes_already_archived(db):
    """Already-archived threads are excluded from candidates."""
    tid_a = db.create_thread("Topic A", session_id="s1")
    tid_b = db.create_thread("Topic B", session_id="s1")
    db.set_current_thread(tid_a)
    db.archive_thread(tid_b)
    candidates = db.get_archive_candidates()
    assert all(c["id"] != tid_b for c in candidates)


def test_get_archive_candidates_background_not_candidate_for_48h_rule(db):
    """Background threads inactive > 48h are NOT candidates (excluded by status filter)."""
    from datetime import datetime, timezone, timedelta

    tid_a = db.create_thread("Topic A", session_id="s1")
    tid_b = db.create_thread("Topic B", session_id="s1")
    db.set_current_thread(tid_a)
    old_time = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    db.update_thread(
        tid_b, status="background", last_active=old_time, agent_task_id="task_1"
    )
    candidates = db.get_archive_candidates()
    assert all(c["id"] != tid_b for c in candidates)


# ------------------------------------------------------------------
# unarchive_thread tests
# ------------------------------------------------------------------


def test_unarchive_thread_restores_show_in_list(db):
    """unarchive_thread sets show_in_list=1."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.archive_thread(tid)
    db.unarchive_thread(tid)
    t = db.get_thread(tid)
    assert t is not None
    assert t["show_in_list"] == 1


def test_unarchive_thread_sets_status_active(db):
    """unarchive_thread sets status='active'."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.archive_thread(tid)
    db.unarchive_thread(tid)
    t = db.get_thread(tid)
    assert t is not None
    assert t["status"] == "active"


def test_unarchive_thread_returns_user_label(db):
    """unarchive_thread return value matches the thread's (new) user_label."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.archive_thread(tid)
    returned_label = db.unarchive_thread(tid)
    t = db.get_thread(tid)
    assert returned_label == t["user_label"]
    assert t["user_label"] is not None


def test_unarchive_thread_full_cycle(db):
    """create → archive → unarchive produces expected state.

    T-slug-wheel: archive KEEPS the slug as a permanent handle; unarchive
    reuses it when no live thread holds it.
    """
    tid = db.create_thread("Topic A", session_id="s1")
    slug = db.get_thread(tid)["user_label"]
    assert slug == "AA"

    db.archive_thread(tid)
    t = db.get_thread(tid)
    assert t["status"] == "archived"
    assert t["user_label"] == slug  # slug persists on archive
    assert t["show_in_list"] == 0

    db.unarchive_thread(tid)
    t = db.get_thread(tid)
    assert t["status"] == "active"
    assert t["show_in_list"] == 1
    assert t["user_label"] == slug  # original slug reused (no live conflict)
