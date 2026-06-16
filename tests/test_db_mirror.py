"""TDD: thread-mirror auto-prunes on terminal transition (T-mirror-prune-on-close).

DEFECT (2026-06-16, manually pruned 3+ times): mirror topics ('~<thread_id>',
is_mirror=1) lingered in the cockpit graph pane after their thread closed or
archived, because no hook pruned them at the moment of the terminal transition.

These tests (temp DB only) pin:
- set_thread_status(... 'closed')   → mirror deleted immediately
- archive_thread(...)               → mirror deleted immediately
- update_thread(status='closed')    → mirror deleted immediately
- a LIVE thread (active/running)    → mirror kept (the feature's value)
- reconcile() prunes mirrors of BOTH closed and archived threads
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_mirror  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    d._set_session_key_external("session_id", "sessA")
    return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project(db, pid="P1") -> str:
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)",
            (pid, f"Project {pid}", "active", _now(), _now()),
        )
        conn.commit()
    return pid


def _mirrored_thread(db, project_id, status="active", topic="do work") -> str:
    """Create an assigned thread and its mirror topic."""
    tid = db.create_thread(topic=topic, session_id="sessA")
    db.update_thread(tid, status=status, project_id=project_id, assigned_by="human")
    db_mirror.mirror_upsert_thread(db, tid, project_id)
    return tid


def _mirror_exists(db, thread_id) -> bool:
    with db._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM graph_topics WHERE thread_id=? AND is_mirror=1",
            (thread_id,),
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Terminal transitions prune the mirror
# ---------------------------------------------------------------------------


def test_set_status_closed_prunes_mirror(db):
    pid = _project(db)
    tid = _mirrored_thread(db, pid)
    assert _mirror_exists(db, tid) is True

    db.set_thread_status(tid, "closed")
    assert _mirror_exists(db, tid) is False


def test_archive_thread_prunes_mirror(db):
    pid = _project(db)
    tid = _mirrored_thread(db, pid)
    assert _mirror_exists(db, tid) is True

    db.archive_thread(tid)
    assert _mirror_exists(db, tid) is False


def test_set_status_archived_prunes_mirror(db):
    pid = _project(db)
    tid = _mirrored_thread(db, pid)

    db.set_thread_status(tid, "archived")
    assert _mirror_exists(db, tid) is False


def test_update_thread_status_closed_prunes_mirror(db):
    pid = _project(db)
    tid = _mirrored_thread(db, pid)

    db.update_thread(tid, status="closed")
    assert _mirror_exists(db, tid) is False


# ---------------------------------------------------------------------------
# Live threads keep their mirror
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["active", "running"])
def test_live_thread_keeps_mirror(db, status):
    pid = _project(db)
    tid = _mirrored_thread(db, pid)

    db.set_thread_status(tid, status)
    assert _mirror_exists(db, tid) is True


# ---------------------------------------------------------------------------
# reconcile prunes BOTH closed and archived
# ---------------------------------------------------------------------------


def test_reconcile_prunes_closed_and_archived(db):
    pid = _project(db)
    live = _mirrored_thread(db, pid, topic="live work")
    closed = _mirrored_thread(db, pid, topic="closed work")
    archived = _mirrored_thread(db, pid, topic="archived work")

    # Flip two terminal WITHOUT the new hook (direct column write) to prove
    # reconcile itself prunes both terminal states.
    with db._connect() as conn:
        conn.execute("UPDATE threads SET status='closed' WHERE id=?", (closed,))
        conn.execute("UPDATE threads SET status='archived' WHERE id=?", (archived,))
        conn.commit()

    result = db_mirror.reconcile(db, pid)

    assert _mirror_exists(db, live) is True
    assert _mirror_exists(db, closed) is False
    assert _mirror_exists(db, archived) is False
    assert result["deleted"] >= 2
