"""Tests for Task 2 thread state helpers."""
import pytest
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


def test_set_thread_status_to_running_updates_last_active(db):
    tid = db.create_thread("t", session_id="s")
    before = db.get_thread(tid)["last_active_at"]
    db.set_thread_status(tid, "running")
    t = db.get_thread(tid)
    assert t["status"] == "running"
    assert t["last_active_at"] >= before


def test_set_thread_status_rejects_invalid(db):
    tid = db.create_thread("t", session_id="s")
    with pytest.raises(ValueError) as e:
        db.set_thread_status(tid, "done")
    assert "invalid status" in str(e.value).lower()


def test_set_thread_status_closed_sets_last_active_now(db):
    tid = db.create_thread("t", session_id="s")
    db.set_thread_status(tid, "closed")
    t = db.get_thread(tid)
    assert t["status"] == "closed"
    assert t["last_active_at"]


def test_touch_last_active_updates_timestamp(db):
    tid = db.create_thread("t", session_id="s")
    before = db.get_thread(tid)["last_active_at"]
    import time
    time.sleep(0.02)
    db.touch_last_active(tid)
    assert db.get_thread(tid)["last_active_at"] >= before


def test_get_threads_by_status(db):
    a = db.create_thread("a", session_id="s")
    b = db.create_thread("b", session_id="s")
    db.set_thread_status(a, "running")
    rs = db.get_threads_by_status("running")
    assert [t["id"] for t in rs] == [a]
    assert db.get_threads_by_status("closed") == []
