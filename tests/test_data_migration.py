"""Tests for Task 9 one-shot lifecycle data migration."""

import json
import pytest
from juggle_db import JuggleDB
from juggle_migrate_lifecycle import migrate


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


def _insert_raw(db, **cols):
    """Insert a thread row with raw status (bypasses set_thread_status guards)."""
    import uuid
    import datetime

    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    tid = cols.get("id") or str(uuid.uuid4())
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO threads (id, session_id, topic, status, "
            "key_decisions, open_questions, last_user_intent, "
            "agent_task_id, agent_result, show_in_list, summarized_msg_count, "
            "created_at, last_active) VALUES "
            "(?, '', ?, ?, '[]', ?, '', NULL, ?, 1, 0, ?, ?)",
            (
                tid,
                cols.get("topic", "t"),
                cols["status"],
                json.dumps(cols.get("open_questions", [])),
                cols.get("agent_result"),
                now,
                now,
            ),
        )
        conn.commit()
    return tid


def test_done_maps_to_closed(db):
    tid = _insert_raw(db, status="done")
    migrate(db)
    assert db.get_thread(tid)["status"] == "closed"


def test_background_maps_to_running(db):
    tid = _insert_raw(db, status="background")
    migrate(db)
    assert db.get_thread(tid)["status"] == "running"


def test_failed_with_open_questions_creates_action_item(db):
    tid = _insert_raw(db, status="failed", open_questions=["Retry?"])
    migrate(db)
    t = db.get_thread(tid)
    assert t["status"] == "closed"
    items = db.get_open_action_items()
    assert any("Retry?" in i["message"] for i in items)
    assert items[0]["priority"] == "high"
    assert items[0]["type"] == "failure"


def test_failed_without_questions_creates_notification(db):
    tid = _insert_raw(db, status="failed", agent_result="timeout")
    migrate(db)
    assert db.get_thread(tid)["status"] == "closed"


def test_needs_action_creates_question_action_item(db):
    tid = _insert_raw(db, status="needs_action", open_questions=["Push?"])
    migrate(db)
    t = db.get_thread(tid)
    assert t["status"] == "closed"
    items = db.get_open_action_items()
    assert any("Push?" in i["message"] and i["type"] == "question" for i in items)


def test_backfill_user_label_in_creation_order(db):
    _insert_raw(db, status="done", topic="first")
    _insert_raw(db, status="active", topic="second")
    migrate(db)
    threads = sorted(db.get_all_threads(), key=lambda t: t["created_at"])
    assert threads[0]["user_label"] == "AA"
    assert threads[1]["user_label"] == "AB"


def test_backfill_last_active_at(db):
    tid = _insert_raw(db, status="active")
    migrate(db)
    assert db.get_thread(tid)["last_active_at"] is not None


def test_idempotent(db):
    _insert_raw(db, status="done")
    migrate(db)
    migrate(db)  # second run should be a no-op
    threads = db.get_all_threads()
    assert all(
        t["status"] in {"active", "running", "closed", "archived"} for t in threads
    )


def test_migration_17_18_19_drops_domain(tmp_path):
    """Migrations 17–19 drop domain columns and tables on an old-schema DB."""
    import sqlite3

    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, domain TEXT)")
    conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, domain TEXT)")
    conn.execute("CREATE TABLE domains (name TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE domain_paths (path_fragment TEXT PRIMARY KEY, domain TEXT)"
    )
    conn.commit()
    conn.close()

    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "src"))
    from juggle_db import JuggleDB

    JuggleDB(db_path).init_db()

    conn2 = sqlite3.connect(db_path)
    cols_threads = {
        row[1] for row in conn2.execute("PRAGMA table_info(threads)").fetchall()
    }
    cols_agents = {
        row[1] for row in conn2.execute("PRAGMA table_info(agents)").fetchall()
    }
    tables = {
        row[0]
        for row in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn2.close()

    assert "domain" not in cols_threads
    assert "domain" not in cols_agents
    assert "domains" not in tables
    assert "domain_paths" not in tables
