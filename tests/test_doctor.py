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
