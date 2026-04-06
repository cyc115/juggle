"""Tests for juggle_context.py ContextBuilder."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB
from juggle_context import ContextBuilder


@pytest.fixture
def active_db(tmp_path):
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    db.set_active(True)
    tid = db.create_thread("Topic A", session_id="s1")
    db.set_current_thread(tid)
    return db


def test_no_stale_flag_when_fresh(active_db):
    # 2 messages — below threshold of 3
    active_db.add_message("A", "user", "real question one")
    active_db.add_message("A", "user", "real question two")
    ctx = ContextBuilder(active_db).build()
    assert "SUMMARY STALE" not in ctx


def test_stale_flag_emitted_at_threshold(active_db):
    for i in range(3):
        active_db.add_message("A", "user", f"real question {i}")
    ctx = ContextBuilder(active_db).build()
    assert "[SUMMARY STALE: 3 new messages" in ctx


def test_stale_flag_not_emitted_after_count_updated(active_db):
    for i in range(3):
        active_db.add_message("A", "user", f"real question {i}")
    active_db.set_summarized_count("A", 3)
    ctx = ContextBuilder(active_db).build()
    assert "SUMMARY STALE" not in ctx


def test_junk_messages_not_counted(active_db):
    active_db.add_message("A", "user", "real question one")
    active_db.add_message("A", "user", "/juggle:show-topics")
    active_db.add_message("A", "user", "<task-notification>...</task-notification>")
    # Only 1 substantive message — below threshold
    ctx = ContextBuilder(active_db).build()
    assert "SUMMARY STALE" not in ctx
