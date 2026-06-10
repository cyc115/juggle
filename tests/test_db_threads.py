"""JuggleDB tests: session active flag, thread CRUD, labels, current thread, UUID + label schema (split from test_juggle_db.py, 2026-06-10)."""

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


def test_is_active_default_false(db):
    assert db.is_active() is False


def test_set_active(db):
    db.set_active(True)
    assert db.is_active() is True
    db.set_active(False)
    assert db.is_active() is False


def test_set_active_sets_started_at_once(db):
    db.set_active(True)
    with db._connect() as conn:
        row1 = conn.execute(
            "SELECT value FROM session WHERE key='started_at'"
        ).fetchone()
    db.set_active(True)
    with db._connect() as conn:
        row2 = conn.execute(
            "SELECT value FROM session WHERE key='started_at'"
        ).fetchone()
    assert row1["value"] == row2["value"]  # not overwritten on second call


def test_create_thread_returns_a(db):
    """create_thread returns a UUID (not a letter)."""
    import re

    tid = db.create_thread("My topic", session_id="s1")
    assert re.match(r"^[0-9a-f-]{36}$", tid), f"Expected UUID, got: {tid}"


def test_create_thread_sequential(db):
    """Sequential threads get sequential user_labels A, B."""
    a = db.create_thread("Topic A", session_id="s1")
    b = db.create_thread("Topic B", session_id="s1")
    assert db.get_thread(a)["user_label"] == "A"
    assert db.get_thread(b)["user_label"] == "B"


def test_create_thread_max_10(db):
    from juggle_db import MAX_THREADS

    for i in range(MAX_THREADS):
        db.create_thread(f"Topic {i}", session_id="s1")
    with pytest.raises(ValueError, match=f"Maximum of {MAX_THREADS}"):
        db.create_thread("Topic overflow", session_id="s1")


def test_get_thread(db):
    tid = db.create_thread("My topic", session_id="s1")
    t = db.get_thread(tid)
    assert t is not None
    assert t["topic"] == "My topic"
    assert t["status"] == "active"


def test_get_thread_missing(db):
    assert db.get_thread("not-a-real-uuid") is None


def test_get_all_threads(db):
    ta = db.create_thread("A topic", session_id="s1")
    tb = db.create_thread("B topic", session_id="s1")
    threads = db.get_all_threads()
    assert len(threads) == 2
    assert threads[0]["id"] == ta
    assert threads[1]["id"] == tb


def test_update_thread(db):
    tid = db.create_thread("My topic", session_id="s1")
    db.update_thread(tid, summary="Updated summary", status="background")
    t = db.get_thread(tid)
    assert t["summary"] == "Updated summary"
    assert t["status"] == "background"


def test_update_thread_list_serialized(db):
    tid = db.create_thread("My topic", session_id="s1")
    db.update_thread(tid, key_decisions=["decision 1", "decision 2"])
    t = db.get_thread(tid)
    # Should be stored as JSON string
    parsed = json.loads(t["key_decisions"])
    assert parsed == ["decision 1", "decision 2"]


def test_set_get_current_thread(db):
    db.create_thread("My topic", session_id="s1")
    assert db.get_current_thread() is None
    db.set_current_thread("A")
    assert db.get_current_thread() == "A"


# UUID + label schema tests
# ------------------------------------------------------------------


def test_create_thread_returns_uuid(db):
    """create_thread() returns a UUID string, not a letter."""
    import re

    tid = db.create_thread("My topic", session_id="s1")
    assert re.match(r"^[0-9a-f-]{36}$", tid), f"Expected UUID, got: {tid}"


def test_create_thread_first_user_label_is_a(db):
    """First thread created gets user_label 'A'."""
    tid = db.create_thread("My topic", session_id="s1")
    thread = db.get_thread(tid)
    assert thread is not None
    assert thread["user_label"] == "A"


def test_create_thread_second_user_label_is_b(db):
    """Second thread gets user_label 'B'."""
    db.create_thread("First", session_id="s1")
    tid2 = db.create_thread("Second", session_id="s1")
    thread = db.get_thread(tid2)
    assert thread["user_label"] == "B"


def test_schema_has_id_and_user_label_not_label(db):
    """threads table has 'id' and 'user_label'; 'label' and 'thread_id' are absent."""
    with db._connect() as conn:
        cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()
        }
    assert "id" in cols
    assert "user_label" in cols
    assert "label" not in cols
    assert "thread_id" not in cols



# ------------------------------------------------------------------
# UUID + label Task 2 tests
# ------------------------------------------------------------------


def test_get_thread_by_uuid(db):
    """get_thread() accepts UUID and returns dict with 'id' and 'user_label'."""
    tid = db.create_thread("My topic", session_id="s1")
    thread = db.get_thread(tid)
    assert thread is not None
    assert thread["id"] == tid
    assert thread["user_label"] == "A"
    assert thread["topic"] == "My topic"


def test_get_all_threads_includes_id_and_user_label(db):
    """get_all_threads() returns dicts with both 'id' and 'user_label'."""
    tid = db.create_thread("Topic A", session_id="s1")
    threads = db.get_all_threads()
    assert len(threads) == 1
    assert threads[0]["id"] == tid
    assert threads[0]["user_label"] == "A"


def test_archive_thread_preserves_user_label(db):
    """archive_thread sets status='archived' and preserves user_label."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.archive_thread(tid)
    thread = db.get_thread(tid)
    assert thread["status"] == "archived"
    assert thread["user_label"] == "A"
    assert thread["show_in_list"] == 0


def test_user_label_not_recycled_after_archive(db):
    """user_label is permanent — archiving thread A does not free 'A' for reuse."""
    tid_a = db.create_thread("First", session_id="s1")
    assert db.get_thread(tid_a)["user_label"] == "A"

    db.archive_thread(tid_a)

    tid_b = db.create_thread("Second", session_id="s1")
    assert db.get_thread(tid_b)["user_label"] == "B"  # B, not A


# ------------------------------------------------------------------
