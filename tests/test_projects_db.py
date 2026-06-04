import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def make_db(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    return db


def test_inbox_seeded(tmp_path):
    db = make_db(tmp_path)
    p = db.get_project("INBOX")
    assert p is not None
    assert p["id"] == "INBOX"


def test_create_project_returns_p_label(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Test", objective="Do a thing")
    assert pid == "P1"
    assert db.get_project(pid)["name"] == "Test"
    assert db.get_project(pid)["status"] == "active"


def test_new_thread_gets_inbox(tmp_path):
    db = make_db(tmp_path)
    tid = db.create_thread("my topic", session_id="s1")
    assert db.get_thread(tid)["project_id"] == "INBOX"


def test_migration_idempotent(tmp_path):
    from juggle_db import JuggleDB
    path = str(tmp_path / "test.db")
    db1 = JuggleDB(path)
    db1.init_db()
    db2 = JuggleDB(path)
    db2.init_db()
    assert db2.get_project("INBOX") is not None


def test_get_active_projects_excludes_inbox(tmp_path):
    db = make_db(tmp_path)
    db.create_project(name="P1", objective="obj1")
    projects = db.get_active_projects()
    assert all(p["id"] != "INBOX" for p in projects)
    assert any(p["name"] == "P1" for p in projects)


def test_count_threads_by_project(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    t1 = db.create_thread("task 1", session_id="s1")
    db.update_thread(t1, project_id=pid)
    assert db.count_threads_by_project(pid) == 1
    assert db.count_threads_by_project("INBOX") == 0


# --- Migration 27 + 28 tests ---

def test_assigned_by_default_auto(tmp_path):
    db = make_db(tmp_path)
    tid = db.create_thread("test topic", session_id="s1")
    assert db.get_thread(tid)["assigned_by"] == "auto"


def test_assigned_by_migration_idempotent(tmp_path):
    from juggle_db import JuggleDB
    path = str(tmp_path / "test.db")
    db1 = JuggleDB(path)
    db1.init_db()
    tid = db1.create_thread("x", session_id="s")
    db2 = JuggleDB(path)
    db2.init_db()
    assert db2.get_thread(tid)["assigned_by"] == "auto"


def test_log_project_correction(tmp_path):
    db = make_db(tmp_path)
    db.log_project_correction("topic A", from_project="INBOX", to_project="P1")
    corrections = db.get_recent_corrections(limit=5)
    assert len(corrections) == 1
    assert corrections[0]["topic"] == "topic A"
    assert corrections[0]["from_project"] == "INBOX"
    assert corrections[0]["to_project"] == "P1"


def test_get_recent_corrections_order(tmp_path):
    db = make_db(tmp_path)
    db.log_project_correction("first", "INBOX", "P1")
    db.log_project_correction("second", "P1", "P2")
    rows = db.get_recent_corrections(limit=5)
    assert rows[0]["topic"] == "second"
    assert rows[1]["topic"] == "first"


def test_get_recent_corrections_limit(tmp_path):
    db = make_db(tmp_path)
    for i in range(7):
        db.log_project_correction(f"topic {i}", "INBOX", "P1")
    assert len(db.get_recent_corrections(limit=5)) == 5


def test_get_human_assigned_threads_by_project(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="work stuff")
    t1 = db.create_thread("topic alpha", session_id="s1")
    t2 = db.create_thread("topic beta", session_id="s1")
    db.update_thread(t1, project_id=pid, assigned_by="human")
    db.update_thread(t2, project_id=pid, assigned_by="auto")
    human_threads = db.get_human_assigned_threads_by_project(pid, limit=3)
    assert len(human_threads) == 1
    assert human_threads[0]["topic"] == "topic alpha"
