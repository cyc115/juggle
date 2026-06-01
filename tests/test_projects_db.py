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
