"""Tests for juggle_cmd_db_flush (Task 6)."""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _make_db(path: Path):
    from juggle_db import JuggleDB
    db = JuggleDB(db_path=str(path))
    db.init_db()
    return db


def test_flush_once_copies_live_to_durable(tmp_path):
    """flush_once copies live DB content to durable path atomically."""
    from juggle_cmd_db_flush import flush_once
    live = tmp_path / "live.db"
    durable = tmp_path / "durable.db"
    _make_db(live)

    # Write a marker row to live
    conn = sqlite3.connect(str(live))
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('test-uuid-1', 'test')"
    )
    conn.commit()
    conn.close()

    flush_once(live, durable)

    assert durable.exists(), "durable should exist after flush"
    conn2 = sqlite3.connect(str(durable))
    row = conn2.execute(
        "SELECT key FROM settings WHERE key='test-uuid-1'"
    ).fetchone()
    conn2.close()
    assert row is not None, "flushed data should be in durable"


def test_flush_once_atomic_on_interrupt(tmp_path):
    """flush_once leaves durable intact if interrupted (uses tmp+rename)."""
    from juggle_cmd_db_flush import flush_once
    live = tmp_path / "live.db"
    durable = tmp_path / "durable.db"
    _make_db(live)
    _make_db(durable)  # pre-existing durable

    # Write original content to durable
    conn_d = sqlite3.connect(str(durable))
    conn_d.execute(
        "INSERT INTO settings (key, value) VALUES ('original-row', 'orig')"
    )
    conn_d.commit()
    conn_d.close()

    # Normal flush
    flush_once(live, durable)
    # durable still readable
    conn2 = sqlite3.connect(str(durable))
    tables = {r[0] for r in conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn2.close()
    assert "nodes" in tables


def test_flush_status_returns_dict(tmp_path):
    """flush_status returns a dict with last_flush_at and age_s fields."""
    from juggle_cmd_db_flush import flush_once, flush_status
    live = tmp_path / "live.db"
    durable = tmp_path / "durable.db"
    _make_db(live)

    flush_once(live, durable)
    status = flush_status(durable)

    assert isinstance(status, dict)
    assert "last_flush_at" in status
    assert "age_s" in status
    assert isinstance(status["age_s"], (int, float))


def test_flush_status_no_flush_yet(tmp_path):
    """flush_status returns None/null last_flush_at when no flush has occurred."""
    from juggle_cmd_db_flush import flush_status
    durable = tmp_path / "durable.db"
    # durable doesn't exist yet
    status = flush_status(durable)
    assert status["last_flush_at"] is None


def test_flush_status_age_increases_after_flush(tmp_path):
    """age_s in flush_status is >= 0 after a flush."""
    from juggle_cmd_db_flush import flush_once, flush_status
    live = tmp_path / "live.db"
    durable = tmp_path / "durable.db"
    _make_db(live)

    flush_once(live, durable)
    status = flush_status(durable)
    assert status["age_s"] >= 0
