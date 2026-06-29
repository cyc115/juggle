"""Tests for juggle_db_bootstrap (Task 4)."""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _make_db(path: Path):
    """Create a minimal juggle DB with one thread row."""
    from juggle_db import JuggleDB
    db = JuggleDB(db_path=str(path))
    db.init_db()
    return db


def test_bootstrap_copies_durable_to_live(tmp_path):
    """bootstrap_tmpfs copies durable DB to live path when live is absent."""
    from juggle_db_bootstrap import bootstrap_tmpfs
    durable = tmp_path / "durable.db"
    live = tmp_path / "shm" / "live.db"
    live.parent.mkdir()
    _make_db(durable)

    bootstrap_tmpfs(live, durable)

    assert live.exists(), "live DB should exist after bootstrap"


def test_bootstrap_live_has_correct_tables(tmp_path):
    """After bootstrap, live DB has the same tables as durable."""
    from juggle_db_bootstrap import bootstrap_tmpfs
    durable = tmp_path / "durable.db"
    live = tmp_path / "shm" / "live.db"
    live.parent.mkdir()
    _make_db(durable)

    bootstrap_tmpfs(live, durable)

    conn = sqlite3.connect(str(live))
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert "nodes" in tables
    assert "messages" in tables


def test_bootstrap_noop_when_live_exists(tmp_path):
    """bootstrap_tmpfs is a noop when live already exists (returns without error)."""
    from juggle_db_bootstrap import bootstrap_tmpfs
    durable = tmp_path / "durable.db"
    live = tmp_path / "shm" / "live.db"
    live.parent.mkdir()
    _make_db(durable)
    _make_db(live)  # live already exists

    mtime_before = live.stat().st_mtime
    bootstrap_tmpfs(live, durable)
    mtime_after = live.stat().st_mtime
    assert mtime_after == mtime_before, "live should not be touched if already exists"


def test_bootstrap_integrity_check_passes(tmp_path):
    """bootstrap_tmpfs runs PRAGMA integrity_check and passes for a valid DB."""
    from juggle_db_bootstrap import bootstrap_tmpfs
    durable = tmp_path / "durable.db"
    live = tmp_path / "shm" / "live.db"
    live.parent.mkdir()
    _make_db(durable)

    # Should not raise
    bootstrap_tmpfs(live, durable)


def test_bootstrap_raises_on_corrupt_durable(tmp_path):
    """bootstrap_tmpfs raises if live DB fails integrity_check after copy."""
    from juggle_db_bootstrap import bootstrap_tmpfs
    import pytest
    durable = tmp_path / "corrupt.db"
    live = tmp_path / "shm" / "live.db"
    live.parent.mkdir()
    # Write garbage so integrity_check fails
    durable.write_bytes(b"this is not a sqlite database at all!!!")

    with pytest.raises(Exception):
        bootstrap_tmpfs(live, durable)
