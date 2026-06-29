"""Tests for Task 1 schema additions (notifications_v2, action_items, threads columns)."""
# fmt: off

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

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
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(notifications_v2)").fetchall()
        }
    assert cols == {"id", "thread_id", "message", "created_at", "session_id"}


def test_action_items_table_exists(db):
    with db._connect() as conn:
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(action_items)").fetchall()
        }
    assert cols == {
        "id",
        "thread_id",
        "message",
        "type",
        "priority",
        "created_at",
        "dismissed_at",
    }


def test_threads_has_user_label_and_last_active_at(db):
    """P8 terminal: the conversation lives in nodes (threads dropped, Migration 55);
    user_label/last_active_at are carried by the kind='conversation' node."""
    with db._connect() as conn:
        cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()
        }
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


# ---------------------------------------------------------------------------
# dismiss_orphan_action_items — regression pin 2026-06-10
# ---------------------------------------------------------------------------


def test_dismiss_orphan_action_items_clears_null_thread(db):
    """dismiss_orphan_action_items() must dismiss only thread_id IS NULL items.

    Regression pin: 2026-06-10 — graph-dispatch orphans (thread_id=None)
    were un-dismissable because action_ack only resolved by thread label.
    """
    t_id = db.create_thread("real thread", session_id="")
    db.add_action_item(t_id, "bound action", type_="question")
    db.add_action_item(None, "orphan 1", type_="question")
    db.add_action_item(None, "orphan 2", type_="question")

    count = db.dismiss_orphan_action_items()

    assert count == 2, f"Expected 2 orphans dismissed, got {count}"
    open_items = db.get_open_action_items()
    assert len(open_items) == 1, "Bound action item must survive"
    assert open_items[0]["thread_id"] == t_id


def test_dismiss_orphan_action_items_skips_already_dismissed(db):
    """Already-dismissed orphans must not be re-counted."""
    import datetime
    from juggle_db import JuggleDB

    db.add_action_item(None, "orphan already done", type_="question")
    with db._connect() as conn:
        conn.execute(
            "UPDATE action_items SET dismissed_at = '2026-01-01 00:00' "
            "WHERE thread_id IS NULL AND dismissed_at IS NULL"
        )
        conn.commit()
    db.add_action_item(None, "new orphan", type_="question")

    count = db.dismiss_orphan_action_items()
    assert count == 1
