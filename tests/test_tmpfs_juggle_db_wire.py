"""Tests for JuggleDB tmpfs bootstrap wiring (Task 5)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_juggldb_direct_mode_unchanged(tmp_path, monkeypatch):
    """JuggleDB in direct mode behaves exactly as before — no tmpfs logic."""
    monkeypatch.delenv("JUGGLE_DB_PATH", raising=False)
    db_path = tmp_path / "juggle.db"
    from juggle_db import JuggleDB
    db = JuggleDB(db_path=str(db_path))
    db.init_db()
    assert db.db_path == db_path


def test_juggledb_tmpfs_mode_uses_live_path(tmp_path, monkeypatch):
    """JuggleDB with mode=tmpfs connects to the live (tmpfs) path, not durable."""
    tmpfs_dir = tmp_path / "shm"
    tmpfs_dir.mkdir()
    durable = tmp_path / "durable.db"

    # Pre-create durable so bootstrap has something to copy
    from juggle_db import JuggleDB
    db_setup = JuggleDB(db_path=str(durable))
    db_setup.init_db()

    monkeypatch.setenv("JUGGLE_DB_PATH", str(durable))

    db = JuggleDB(
        db_path=str(durable),
        _tmpfs_mode=True,
        _tmpfs_dir=str(tmpfs_dir),
        _instance_id="testinst",
        _platform="linux",
    )
    assert "testinst" in str(db.db_path), (
        f"Expected tmpfs live path with instance id, got {db.db_path}"
    )
    assert db.db_path.parent == tmpfs_dir


def test_juggledb_tmpfs_creates_live_from_durable(tmp_path, monkeypatch):
    """JuggleDB tmpfs mode bootstraps live from durable when live is absent."""
    tmpfs_dir = tmp_path / "shm"
    tmpfs_dir.mkdir()
    durable = tmp_path / "durable.db"

    from juggle_db import JuggleDB
    db_setup = JuggleDB(db_path=str(durable))
    db_setup.init_db()

    db = JuggleDB(
        db_path=str(durable),
        _tmpfs_mode=True,
        _tmpfs_dir=str(tmpfs_dir),
        _instance_id="inst2",
        _platform="linux",
    )
    assert db.db_path.exists(), "Live DB should be created by bootstrap"
