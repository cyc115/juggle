"""Tests for paths.vault + paths.vault_name config reading."""

import sys
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_get_vault_root_from_paths_vault():
    """_get_vault_root() reads paths.vault from settings (resolution now lives in
    juggle_vault_paths — spec §1.2, P0a 2026-07-04)."""
    with patch(
        "juggle_vault_paths.get_settings",
        return_value={
            "paths": {"vault": "/Documents/test-vault", "vault_name": ""},
        },
    ):
        from juggle_cli import _get_vault_root

        root = _get_vault_root()
        assert root == Path.home() / "Documents/test-vault"


def test_get_vault_name_explicit():
    """_get_vault_name() returns explicit vault_name when set."""
    with patch(
        "juggle_cli.get_settings",
        return_value={
            "paths": {"vault": "/Documents/personal", "vault_name": "MyVault"},
        },
    ):
        from juggle_cli import _get_vault_name

        assert _get_vault_name() == "MyVault"


def test_get_vault_name_derived_from_path():
    """_get_vault_name() derives name from vault path when vault_name is empty.

    Name read stays in juggle_cli; root resolution moved to juggle_vault_paths
    (P0a 2026-07-04) — patch both so the derived-name path is exercised."""
    settings = {"paths": {"vault": "/Documents/personal", "vault_name": ""}}
    with patch("juggle_cli.get_settings", return_value=settings), patch(
        "juggle_vault_paths.get_settings", return_value=settings
    ):
        from juggle_cli import _get_vault_name

        assert _get_vault_name() == "personal"


def test_get_vault_info_research():
    """_get_vault_info() in juggle_cmd_research reads paths.vault.

    Root now resolves via juggle_vault_paths; vault_name stays a local
    juggle_cmd_research read (P0a 2026-07-04) — patch both."""
    settings = {"paths": {"vault": "/Documents/personal", "vault_name": "personal"}}
    with patch("juggle_cmd_research.get_settings", return_value=settings), patch(
        "juggle_vault_paths.get_settings", return_value=settings
    ):
        from juggle_cmd_research import _get_vault_info

        vault_path, vault_name = _get_vault_info()
        assert vault_path.endswith("/Documents/personal")
        assert vault_name == "personal"


def test_get_vault_root_tilde_path():
    """_get_vault_root() expands tilde-prefixed vault values (resolution in
    juggle_vault_paths — P0a 2026-07-04)."""
    with patch(
        "juggle_vault_paths.get_settings",
        return_value={
            "paths": {"vault": "~/Documents/personal", "vault_name": ""},
        },
    ):
        from juggle_cli import _get_vault_root

        root = _get_vault_root()
        assert root == Path.home() / "Documents" / "personal"


def test_migrate_config_deep_copy():
    """_migrate_config does not mutate the input dict."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from juggle_cmd_doctor import _migrate_config

    original = {
        "domains": {"initial_domain_paths": [["/Documents/personal", "vault"]]},
        "paths": {},
    }
    _migrate_config(original)
    # input should not have been mutated
    assert "domains" in original
    assert original["paths"] == {}
