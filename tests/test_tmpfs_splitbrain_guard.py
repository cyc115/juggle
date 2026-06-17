"""Tests for split-brain guard (Task 7).

If db.mode=tmpfs and tmpfs_dir is missing or unwritable, JuggleDB must
hard-fail at startup rather than silently falling back or corrupting data.
"""
import os
import sys
import pytest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_splitbrain_guard_raises_when_tmpfs_dir_missing(tmp_path):
    """JuggleDB raises if mode=tmpfs and tmpfs_dir does not exist."""
    from juggle_db_path import resolve_db_paths
    durable = tmp_path / "juggle.db"
    missing_dir = tmp_path / "nonexistent_shm"
    # missing_dir is not created

    with pytest.raises(FileNotFoundError):
        _check_tmpfs_writable(str(missing_dir))


def test_splitbrain_guard_raises_when_tmpfs_dir_unwritable(tmp_path):
    """JuggleDB raises if mode=tmpfs and tmpfs_dir is not writable."""
    from juggle_db_path import resolve_db_paths
    durable = tmp_path / "juggle.db"
    locked_dir = tmp_path / "locked_shm"
    locked_dir.mkdir(mode=0o444)  # read-only

    try:
        with pytest.raises(PermissionError):
            _check_tmpfs_writable(str(locked_dir))
    finally:
        locked_dir.chmod(0o755)  # restore for cleanup


def test_splitbrain_guard_passes_for_writable_dir(tmp_path):
    """_check_tmpfs_writable does not raise for a writable dir."""
    writable = tmp_path / "shm"
    writable.mkdir()
    _check_tmpfs_writable(str(writable))  # should not raise


def _check_tmpfs_writable(tmpfs_dir: str) -> None:
    """Import and call the guard function."""
    from juggle_db_path import check_tmpfs_writable
    check_tmpfs_writable(tmpfs_dir)
