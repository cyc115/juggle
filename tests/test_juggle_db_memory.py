"""Tests for memory-related DB columns on threads."""
import sys
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


def test_thread_has_memory_loaded_default_false(db):
    tid = db.create_thread("test topic", session_id="")
    thread = db.get_thread(tid)
    assert thread["memory_loaded"] == 0  # SQLite stores bool as int


def test_thread_has_memory_context_default_empty(db):
    tid = db.create_thread("test topic", session_id="")
    thread = db.get_thread(tid)
    assert thread["memory_context"] == ""


def test_update_memory_context(db):
    tid = db.create_thread("test topic", session_id="")
    db.update_thread(tid, memory_context="recalled fact 1\nrecalled fact 2", memory_loaded=1)
    thread = db.get_thread(tid)
    assert thread["memory_context"] == "recalled fact 1\nrecalled fact 2"
    assert thread["memory_loaded"] == 1


def test_memory_loaded_survives_other_updates(db):
    tid = db.create_thread("test topic", session_id="")
    db.update_thread(tid, memory_context="context", memory_loaded=1)
    db.update_thread(tid, summary="new summary")
    thread = db.get_thread(tid)
    assert thread["memory_loaded"] == 1
    assert thread["memory_context"] == "context"
