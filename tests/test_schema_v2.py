"""Tests for Task 1 schema additions (notifications_v2, action_items, threads columns)."""
import tempfile
from pathlib import Path

import pytest

from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "juggle.db"
    d = JuggleDB(db_path=str(path))
    d.init_db()
    return d


def test_notifications_v2_table_exists(db):
    with db._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(notifications_v2)").fetchall()}
    assert cols == {"id", "thread_id", "message", "created_at", "session_id"}


def test_action_items_table_exists(db):
    with db._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(action_items)").fetchall()}
    assert cols == {"id", "thread_id", "message", "type", "priority", "created_at", "dismissed_at"}


def test_threads_has_user_label_and_last_active_at(db):
    with db._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()}
    assert "user_label" in cols
    assert "last_active_at" in cols


def test_user_label_unique_constraint(db):
    tid1 = db.create_thread("t1", session_id="s")
    tid2 = db.create_thread("t2", session_id="s")
    t1 = db.get_thread(tid1)
    t2 = db.get_thread(tid2)
    assert t1["user_label"] != t2["user_label"]


def test_thread_auto_archive_ttl_setting_seeded(db):
    with db._connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'thread_auto_archive_ttl_secs'"
        ).fetchone()
    assert row is not None
    assert row["value"] == "3600"


def test_action_items_open_index_filters_dismissed(db):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO action_items (thread_id, message, type, priority, created_at) "
            "VALUES (NULL, 'open', 'question', 'normal', '2026-04-18 10:00')"
        )
        conn.execute(
            "INSERT INTO action_items (thread_id, message, type, priority, created_at, dismissed_at) "
            "VALUES (NULL, 'done', 'question', 'normal', '2026-04-18 10:00', '2026-04-18 11:00')"
        )
        conn.commit()
        rows = conn.execute(
            "SELECT message FROM action_items WHERE dismissed_at IS NULL"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["message"] == "open"
