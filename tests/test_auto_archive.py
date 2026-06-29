"""Tests for Task 7 auto-archive hook."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from datetime import datetime, timezone, timedelta
import pytest
from juggle_db import JuggleDB
from juggle_context import _auto_archive_closed_threads


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


def _set_last_active(db, tid, dt):
    s = dt.strftime("%Y-%m-%d %H:%M")
    with db._connect() as conn:
        # _auto_archive_closed_threads reads the conversation node (P8 Task 4.2;
        # legacy threads dropped).
        conn.execute("UPDATE nodes SET last_active_at = ? WHERE id = ? "
                     "AND kind='conversation'", (s, tid))
        conn.commit()


def test_stale_closed_thread_archives(db):
    tid = db.create_thread("t", session_id="s")
    db.set_thread_status(tid, "closed")
    old = datetime.now(timezone.utc) - timedelta(seconds=86400 + 60)
    _set_last_active(db, tid, old)
    _auto_archive_closed_threads(db)
    assert db.get_thread(tid)["state"] == "archived"


def test_fresh_closed_thread_stays_closed(db):
    tid = db.create_thread("t", session_id="s")
    db.set_thread_status(tid, "closed")
    _auto_archive_closed_threads(db)
    assert db.get_thread(tid)["state"] == "done"


def test_idempotent(db):
    tid = db.create_thread("t", session_id="s")
    db.set_thread_status(tid, "closed")
    old = datetime.now(timezone.utc) - timedelta(seconds=86400 + 60)
    _set_last_active(db, tid, old)
    _auto_archive_closed_threads(db)
    _auto_archive_closed_threads(db)  # no-op second run
    assert db.get_thread(tid)["state"] == "archived"


def test_preserves_user_label(db):
    tid = db.create_thread("t", session_id="s")
    slug = db.get_thread(tid)["user_label"]
    db.set_thread_status(tid, "closed")
    old = datetime.now(timezone.utc) - timedelta(seconds=86400 + 60)
    _set_last_active(db, tid, old)
    _auto_archive_closed_threads(db)
    # T-slug-wheel: slug persists through close + auto-archive.
    assert db.get_thread(tid)["user_label"] == slug
