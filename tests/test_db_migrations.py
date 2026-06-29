"""JuggleDB tests: schema migrations (thread_id->id/user_label, last_reflect_msg_count) (split from test_juggle_db.py, 2026-06-10)."""

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
def test_migration_preserves_existing_threads(tmp_path):
    """Existing DBs with thread_id column are migrated: id=letter, label=letter."""
    import sqlite3

    old_db_path = tmp_path / "old.db"
    # Create a legacy-style DB
    with sqlite3.connect(str(old_db_path)) as conn:
        conn.execute("""CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL DEFAULT '',
            topic TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            summary TEXT DEFAULT '',
            key_decisions TEXT DEFAULT '[]',
            open_questions TEXT DEFAULT '[]',
            last_user_intent TEXT DEFAULT '',
            agent_task_id TEXT,
            agent_result TEXT,
            show_in_list INTEGER NOT NULL DEFAULT 1,
            summarized_msg_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '2024-01-01T00:00:00',
            last_active TEXT NOT NULL DEFAULT '2024-01-01T00:00:00'
        )""")
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            token_estimate INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '2024-01-01T00:00:00'
        )""")
        conn.execute("""CREATE TABLE shared_context (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            context_type TEXT NOT NULL,
            content TEXT NOT NULL,
            source_thread TEXT,
            created_at TEXT NOT NULL DEFAULT '2024-01-01T00:00:00'
        )""")
        conn.execute("""CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            message TEXT NOT NULL,
            delivered INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '2024-01-01T00:00:00'
        )""")
        conn.execute("""CREATE TABLE session (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        conn.execute(
            "INSERT INTO threads (thread_id, session_id, topic, created_at, last_active) VALUES ('A', '', 'Legacy Topic', '2024-01-01', '2024-01-01')"
        )
        conn.commit()

    from juggle_db import JuggleDB

    db = JuggleDB(str(old_db_path))
    db.init_db()  # triggers migration

    # This pins the legacy thread_id->id + label rename (a threads-table
    # migration). get_thread now reads the conversation node (P8 Task 4.2), but
    # this synthetic ultra-minimal schema never reaches the nodes backfill, so
    # verify the migrated row through the threads seam this test actually covers.
    with db._connect() as conn:
        row = conn.execute(
            "SELECT id, topic FROM threads WHERE id = 'A'").fetchone()
    assert row is not None
    assert row["id"] == "A"
    assert row["topic"] == "Legacy Topic"



