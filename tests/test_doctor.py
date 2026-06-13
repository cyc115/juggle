"""Tests for juggle doctor config migration helper."""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_cmd_doctor import _migrate_config  # noqa: E402


def test_migrate_config_moves_vault_path():
    cfg = {
        "paths": {"data_dir": "~/.claude/juggle"},
        "domains": {
            "initial_domains": ["juggle", "vault"],
            "initial_domain_paths": [
                ["/github/juggle", "juggle"],
                ["/Documents/my-vault", "vault"],
            ],
            "vault_name": "MyVault",
        },
    }
    new_cfg, changes = _migrate_config(dict(cfg))
    assert new_cfg["paths"]["vault"] == "/Documents/my-vault"
    assert new_cfg["paths"]["vault_name"] == "MyVault"
    assert "domains" not in new_cfg
    assert len(changes) >= 2


def test_migrate_config_preserves_existing_paths_vault():
    """If user has already set paths.vault, do not overwrite it."""
    cfg = {
        "paths": {"vault": "/Documents/already-set"},
        "domains": {
            "initial_domain_paths": [["/Documents/should-not-use", "vault"]],
            "vault_name": "ShouldNotUse",
        },
    }
    new_cfg, changes = _migrate_config(dict(cfg))
    assert new_cfg["paths"]["vault"] == "/Documents/already-set"
    assert "domains" not in new_cfg


def test_migrate_config_no_op_when_no_domains_block():
    cfg = {"paths": {"vault": "/Documents/personal"}}
    new_cfg, changes = _migrate_config(dict(cfg))
    assert new_cfg == cfg
    assert changes == []


def test_migrate_config_handles_missing_vault_entry():
    """domains block without a 'vault' path: still strip block, leave paths alone."""
    cfg = {
        "paths": {},
        "domains": {"initial_domain_paths": [["/github/juggle", "juggle"]]},
    }
    new_cfg, changes = _migrate_config(dict(cfg))
    assert "domains" not in new_cfg
    assert "vault" not in new_cfg["paths"]


def test_doctor_migrates_node_era_db(tmp_path, monkeypatch, capsys):
    """REGRESSION PIN (2026-06-13): doctor's presence-based detection only
    looked for the stale domain schema, so a node-era graph_nodes DB printed
    'already on 1.21.0' and never got the node->task rename. doctor must detect
    graph_nodes and run init_db to apply Migration 39."""
    import sqlite3

    import juggle_cmd_doctor as doc
    import juggle_db

    db_path = tmp_path / "node_era.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE graph_nodes (id TEXT PRIMARY KEY, project_id TEXT, title TEXT,"
        " prompt TEXT, verify_cmd TEXT, state TEXT, thread_id TEXT, handoff TEXT,"
        " diffstat TEXT, verified_at TEXT, created_at TEXT, updated_at TEXT, topic_id TEXT)"
    )
    conn.execute(
        "INSERT INTO graph_nodes (id, project_id, title, prompt, state, created_at,"
        " updated_at) VALUES ('z','INBOX','Z','p','pending','t','t')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(juggle_db, "DB_PATH", str(db_path))
    monkeypatch.setattr(doc, "CONFIG_PATH", tmp_path / "nope.json")

    class _Args:
        dry_run = False

    assert doc.cmd_doctor(_Args()) == 0
    assert "Migration 39" in capsys.readouterr().out

    conn = sqlite3.connect(str(db_path))
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    ids = {r[0] for r in conn.execute("SELECT id FROM graph_tasks")}
    conn.close()
    assert "graph_nodes" not in tables and "graph_tasks" in tables
    assert ids == {"z"}, "node-era rows must migrate into graph_tasks, not be lost"
