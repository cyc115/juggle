"""Tests for the vault-path / vault-name CLI commands.

These wrap _get_vault_root() / _get_vault_name() so slash commands can resolve
the vault via a single source of truth instead of duplicated inline python.
"""

import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import juggle_cli


def test_cmd_vault_path_prints_root(capsys, monkeypatch):
    monkeypatch.setattr(juggle_cli, "_get_vault_root", lambda: Path("/home/u/Vault"))
    juggle_cli.cmd_vault_path(None)
    assert capsys.readouterr().out.strip() == "/home/u/Vault"


def test_cmd_vault_name_prints_name(capsys, monkeypatch):
    monkeypatch.setattr(juggle_cli, "_get_vault_name", lambda: "MyVault")
    juggle_cli.cmd_vault_name(None)
    assert capsys.readouterr().out.strip() == "MyVault"


def test_vault_root_handles_tilde_prefix(monkeypatch):
    # The old inline `expanduser('~') + vault_rel` mishandled this; the real
    # function must expand a ~-prefixed config correctly.
    monkeypatch.setattr(juggle_cli, "get_settings", lambda: {"paths": {"vault": "~/Notes"}})
    assert juggle_cli._get_vault_root() == Path.home() / "Notes"


def test_vault_root_handles_leading_slash(monkeypatch):
    monkeypatch.setattr(
        juggle_cli, "get_settings", lambda: {"paths": {"vault": "/Documents/personal"}}
    )
    assert juggle_cli._get_vault_root() == Path.home() / "Documents/personal"


def test_vault_name_prefers_explicit_then_falls_back(monkeypatch):
    monkeypatch.setattr(
        juggle_cli,
        "get_settings",
        lambda: {"paths": {"vault": "/Documents/personal", "vault_name": "Brain"}},
    )
    assert juggle_cli._get_vault_name() == "Brain"

    monkeypatch.setattr(
        juggle_cli, "get_settings", lambda: {"paths": {"vault": "/Documents/personal"}}
    )
    assert juggle_cli._get_vault_name() == "personal"
