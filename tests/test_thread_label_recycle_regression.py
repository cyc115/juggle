"""Regression tests for thread label accumulation bug (2026-06-15).

Original symptom: archived/closed threads retained non-null user_label, causing
UNIQUE constraint failures when create_thread picked a "free" label still held by
a stale archived/closed row.

Resolution (T-slug-wheel): slugs are now PERMANENT historical handles that
PERSIST on close/archive. Accumulation can no longer cause UNIQUE failures
because (a) every NON-ARCHIVED thread is constrained (partial unique index
idx_threads_live_label; widened again 2026-07-08 to cover 'done'/'failed-exec',
not just open/running/background) and (b) the rotating wheel skips
non-archived-held slugs at allocation. So pins 1-4 flip from "NULL on
terminal" to "slug persists", while 5-6 still hold: creation never raises
UNIQUE, and two non-archived threads never share a slug.

Pin 5 was rewritten 2026-07-08 (incident: a 'done'-but-unarchived thread's
slug was silently recycled to a brand new thread, producing two non-archived
rows sharing a label — see tests/test_thread_label_alloc_atomic.py). A closed/
done row now holds its slug (like a live row) until explicitly archived, so
"reuses the slug from the closed row" is no longer the guaranteed behavior —
only "never raises UNIQUE, and gets a DIFFERENT slug" is.

Pins:
1. set_thread_status('closed') KEEPS user_label.
2. set_thread_status('archived') KEEPS user_label.
3. archive_thread() KEEPS user_label.
4. update_thread(status='closed') KEEPS user_label.
5. create_thread is collision-robust — never raises UNIQUE error when the wheel
   revisits a closed-but-unarchived holder's slug; it skips to a fresh one.
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
# 1. set_thread_status('closed') KEEPS user_label (T-slug-wheel persistence)
# ---------------------------------------------------------------------------
def test_set_thread_status_closed_keeps_label(db):
    """T-slug-wheel: close-thread keeps the slug as a permanent handle."""
    tid = db.create_thread("topic", session_id="s1")
    slug = db.get_thread(tid)["user_label"]
    assert slug is not None

    db.set_thread_status(tid, "closed")

    assert db.get_thread(tid)["user_label"] == slug


# ---------------------------------------------------------------------------
# 2. set_thread_status('archived') KEEPS user_label
# ---------------------------------------------------------------------------
def test_set_thread_status_archived_keeps_label(db):
    """T-slug-wheel: archive via set_thread_status keeps the slug."""
    tid = db.create_thread("topic", session_id="s1")
    slug = db.get_thread(tid)["user_label"]
    db.set_thread_status(tid, "archived")
    assert db.get_thread(tid)["user_label"] == slug


# ---------------------------------------------------------------------------
# 3. archive_thread() KEEPS user_label
# ---------------------------------------------------------------------------
def test_archive_thread_keeps_label(db):
    tid = db.create_thread("topic", session_id="s1")
    slug = db.get_thread(tid)["user_label"]
    db.archive_thread(tid)
    assert db.get_thread(tid)["user_label"] == slug


# ---------------------------------------------------------------------------
# 4. update_thread(status='closed') KEEPS user_label
# ---------------------------------------------------------------------------
def test_update_thread_closed_keeps_label(db):
    """update_thread with status=closed must keep user_label."""
    tid = db.create_thread("topic", session_id="s1")
    slug = db.get_thread(tid)["user_label"]
    db.update_thread(tid, status="closed")
    assert db.get_thread(tid)["user_label"] == slug


# ---------------------------------------------------------------------------
# 5. create_thread is collision-robust: a terminal row holding a slug must not
#    block creation of a new thread. NO UNIQUE constraint error raised.
# ---------------------------------------------------------------------------
def test_create_thread_robust_to_terminal_label_collision(db):
    """A closed (done)-but-unarchived row holding a slug must not block
    creation of a new thread whose wheel position would land on that slug —
    the wheel just skips past it to a fresh one (2026-07-08: closed/done rows
    hold their slug like a live row until archived; the old "reuses the slug"
    expectation was the incident itself — see test_thread_label_alloc_atomic.
    test_done_but_unarchived_label_not_recycled_to_new_thread)."""
    tid = db.create_thread("stale topic", session_id="s1")
    held = db.get_thread(tid)["user_label"]
    db.set_thread_status(tid, "closed")

    # Force the wheel back so the next allocation targets the held slug; the
    # closed holder must not cause a UNIQUE failure.
    from dbops.schema import _wheel_index

    with db._connect() as conn:
        conn.execute(
            "INSERT INTO juggle_meta(key, value) VALUES ('label_seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(_wheel_index(held)),),
        )
        conn.commit()

    # Must not raise UNIQUE constraint error, and must NOT reuse the still-held slug.
    new_id = db.create_thread("new topic", session_id="s1")
    new_thread = db.get_thread(new_id)
    assert new_thread is not None
    assert new_thread["state"] == "open"
    assert new_thread["user_label"] != held  # closed/done still holds its slug


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
