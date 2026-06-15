"""Regression tests for thread label accumulation bug (2026-06-15).

Symptom: archived/closed threads retained non-null user_label, causing UNIQUE
constraint failures when create_thread picked a "free" label that was still
physically held by a stale archived/closed row.

Pins:
1. set_thread_status('closed') NULLs user_label.
2. set_thread_status('archived') NULLs user_label.
3. archive_thread() NULLs user_label.
4. update_thread(status='closed') NULLs user_label.
5. create_thread is collision-robust — never raises UNIQUE error on stale holder.
6. Two active threads never share a label.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


# ---------------------------------------------------------------------------
# 1. set_thread_status('closed') NULLs user_label
# ---------------------------------------------------------------------------
def test_set_thread_status_closed_nulls_label(db):
    """Incident 2026-06-15: close-thread left user_label set → accumulation."""
    tid = db.create_thread("topic", session_id="s1")
    assert db.get_thread(tid)["user_label"] is not None

    db.set_thread_status(tid, "closed")

    assert db.get_thread(tid)["user_label"] is None


# ---------------------------------------------------------------------------
# 2. set_thread_status('archived') NULLs user_label
# ---------------------------------------------------------------------------
def test_set_thread_status_archived_nulls_label(db):
    """Incident 2026-06-15: archive via set_thread_status must NULL label."""
    tid = db.create_thread("topic", session_id="s1")
    db.set_thread_status(tid, "archived")
    assert db.get_thread(tid)["user_label"] is None


# ---------------------------------------------------------------------------
# 3. archive_thread() NULLs user_label
# ---------------------------------------------------------------------------
def test_archive_thread_nulls_label(db):
    tid = db.create_thread("topic", session_id="s1")
    db.archive_thread(tid)
    assert db.get_thread(tid)["user_label"] is None


# ---------------------------------------------------------------------------
# 4. update_thread(status='closed') NULLs user_label
# ---------------------------------------------------------------------------
def test_update_thread_closed_nulls_label(db):
    """update_thread with status=closed must clear user_label."""
    tid = db.create_thread("topic", session_id="s1")
    db.update_thread(tid, status="closed")
    assert db.get_thread(tid)["user_label"] is None


# ---------------------------------------------------------------------------
# 5. create_thread is collision-robust: stale closed row holds label 'A'
#    → new thread must succeed. NO UNIQUE constraint error raised.
# ---------------------------------------------------------------------------
def test_create_thread_robust_to_stale_label_collision(db):
    """Incident 2026-06-15: stale closed row holding label 'A' must not
    block creation of a new thread whose label generator also picks 'A'."""
    tid = db.create_thread("stale topic", session_id="s1")
    db.set_thread_status(tid, "closed")

    # Simulate the pre-fix bug: label still set on the closed row.
    with db._connect() as conn:
        conn.execute("UPDATE threads SET user_label = 'A' WHERE id = ?", (tid,))
        conn.commit()

    # Must not raise UNIQUE constraint error.
    new_id = db.create_thread("new topic", session_id="s1")
    new_thread = db.get_thread(new_id)
    assert new_thread is not None
    assert new_thread["status"] == "active"
    assert new_thread["user_label"] is not None


# ---------------------------------------------------------------------------
# 6. Two active threads never share a label (regression guard).
# ---------------------------------------------------------------------------
def test_two_active_threads_never_share_label(db):
    a = db.create_thread("Topic A", session_id="s1")
    b = db.create_thread("Topic B", session_id="s1")
    la = db.get_thread(a)["user_label"]
    lb = db.get_thread(b)["user_label"]
    assert la != lb
    assert la is not None
    assert lb is not None
