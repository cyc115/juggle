"""Tests for juggle_db_path resolver (Task 2)."""
import os
import platform
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_direct_mode_live_equals_durable(tmp_path):
    """Direct mode: live_path == durable_path == provided path."""
    from juggle_db_path import resolve_db_paths
    durable = tmp_path / "juggle.db"
    result = resolve_db_paths("direct", "/dev/shm", durable, "test")
    assert result.live == durable
    assert result.durable == durable
    assert result.mode == "direct"


def test_tmpfs_mode_linux_live_in_tmpfs_dir(tmp_path):
    """tmpfs mode on Linux: live_path is under tmpfs_dir."""
    from juggle_db_path import resolve_db_paths
    durable = tmp_path / "juggle.db"
    tmpfs_dir = tmp_path / "shm"
    tmpfs_dir.mkdir()
    result = resolve_db_paths("tmpfs", str(tmpfs_dir), durable, "test-inst",
                              _platform="linux")
    assert result.mode == "tmpfs"
    assert result.live.parent == tmpfs_dir
    assert result.live != durable
    assert result.durable == durable


def test_tmpfs_mode_live_name_includes_instance(tmp_path):
    """tmpfs live path includes the instance id."""
    from juggle_db_path import resolve_db_paths
    durable = tmp_path / "juggle.db"
    tmpfs_dir = tmp_path / "shm"
    tmpfs_dir.mkdir()
    result = resolve_db_paths("tmpfs", str(tmpfs_dir), durable, "myinstance",
                              _platform="linux")
    assert "myinstance" in result.live.name


def test_tmpfs_mode_macos_falls_back_to_direct(tmp_path):
    """tmpfs mode on macOS falls back to direct mode with a warning."""
    from juggle_db_path import resolve_db_paths
    import logging
    durable = tmp_path / "juggle.db"
    result = resolve_db_paths("tmpfs", "/dev/shm", durable, "test",
                              _platform="darwin")
    assert result.mode == "direct"
    assert result.live == durable
    assert result.durable == durable


def test_dbpaths_has_expected_fields(tmp_path):
    """DbPaths dataclass has live, durable, mode fields."""
    from juggle_db_path import resolve_db_paths, DbPaths
    durable = tmp_path / "juggle.db"
    result = resolve_db_paths("direct", "/dev/shm", durable, "x")
    assert isinstance(result, DbPaths)
    assert hasattr(result, "live")
    assert hasattr(result, "durable")
    assert hasattr(result, "mode")


def test_unknown_mode_raises(tmp_path):
    """Unknown mode raises ValueError."""
    from juggle_db_path import resolve_db_paths
    import pytest
    durable = tmp_path / "juggle.db"
    with pytest.raises(ValueError, match="mode"):
        resolve_db_paths("invalid", "/dev/shm", durable, "x")
