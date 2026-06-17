"""Tests for tmpfs db-mode settings defaults (Task 1)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_db_section_exists_in_defaults():
    """DEFAULTS must have a 'db' section."""
    from juggle_settings import DEFAULTS
    assert "db" in DEFAULTS, "DEFAULTS must have 'db' section"


def test_db_mode_default_is_direct():
    """db.mode defaults to 'direct'."""
    from juggle_settings import DEFAULTS
    assert DEFAULTS["db"]["mode"] == "direct"


def test_db_tmpfs_dir_default():
    """db.tmpfs_dir defaults to /dev/shm."""
    from juggle_settings import DEFAULTS
    assert DEFAULTS["db"]["tmpfs_dir"] == "/dev/shm"


def test_db_flush_interval_default():
    """db.flush_interval_s defaults to 10."""
    from juggle_settings import DEFAULTS
    assert DEFAULTS["db"]["flush_interval_s"] == 10


def test_get_settings_returns_db_section():
    """get_settings() returns the db section with correct defaults."""
    from juggle_settings import get_settings
    s = get_settings()
    assert s["db"]["mode"] == "direct"
    assert s["db"]["tmpfs_dir"] == "/dev/shm"
    assert s["db"]["flush_interval_s"] == 10
