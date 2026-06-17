"""Tests for db-mode init integration (Task 8)."""
import os
import sys
import json
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_configure_db_mode_writes_config(tmp_path, monkeypatch):
    """configure_db_mode writes db.mode to config.json idempotently."""
    from juggle_cmd_db_flush import configure_db_mode
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(config_path))

    configure_db_mode("tmpfs", config_path=config_path)

    cfg = json.loads(config_path.read_text())
    assert cfg["db"]["mode"] == "tmpfs"


def test_configure_db_mode_idempotent(tmp_path, monkeypatch):
    """configure_db_mode can be called twice without error."""
    from juggle_cmd_db_flush import configure_db_mode
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(config_path))

    configure_db_mode("tmpfs", config_path=config_path)
    configure_db_mode("tmpfs", config_path=config_path)

    cfg = json.loads(config_path.read_text())
    assert cfg["db"]["mode"] == "tmpfs"


def test_configure_db_mode_preserves_existing_keys(tmp_path, monkeypatch):
    """configure_db_mode preserves other keys in config.json."""
    from juggle_cmd_db_flush import configure_db_mode
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"max_threads": 5}))
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(config_path))

    configure_db_mode("direct", config_path=config_path)

    cfg = json.loads(config_path.read_text())
    assert cfg["max_threads"] == 5
    assert cfg["db"]["mode"] == "direct"
