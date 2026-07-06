"""tests/test_vcs_configure.py — vcs-backend-init step 3 writes the SAME repo
config shape juggle_repo_vcs reads (no parallel schema): the writer lives beside
the reader so they can't drift.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from juggle_repo_vcs import (  # noqa: E402
    repo_async_land,
    repo_trunk,
    write_repo_vcs_config,
)
from juggle_settings import get_repo_config  # noqa: E402


class _Backend:
    class capabilities:  # noqa: N801 — attribute holder, not a real class use
        async_land = False


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(path))
    return path


def test_write_repo_vcs_config_roundtrips(_cfg):
    repo = "/abs/some/repo"
    write_repo_vcs_config(
        repo,
        vcs="sapling",
        trunk="remote/main",
        async_land=True,
        submit_cmd="jf submit",
        land_status_cmd="sl smartlog",
    )

    assert get_repo_config(repo)["vcs"] == "sapling"
    assert repo_trunk(repo) == "remote/main"
    # config override wins over the backend's own (False) capability
    assert repo_async_land(repo, _Backend()) is True
    block = json.loads(_cfg.read_text())["repos"][repo]
    assert block["submit_cmd"] == "jf submit"
    assert block["land_status_cmd"] == "sl smartlog"


def test_write_preserves_other_config(_cfg):
    _cfg.write_text(json.dumps({"max_agents": 7, "repos": {"/other": {"vcs": "git"}}}))
    write_repo_vcs_config("/abs/repo", vcs="toy", trunk="main", async_land=False)

    cfg = json.loads(_cfg.read_text())
    assert cfg["max_agents"] == 7
    assert cfg["repos"]["/other"]["vcs"] == "git"
    assert cfg["repos"]["/abs/repo"]["vcs"] == "toy"


def test_write_git_omits_empty_cmds(_cfg):
    write_repo_vcs_config("/abs/repo", vcs="git", trunk="main", async_land=False)
    block = json.loads(_cfg.read_text())["repos"]["/abs/repo"]
    assert "submit_cmd" not in block
    assert "land_status_cmd" not in block
    assert block == {"vcs": "git", "trunk": "main", "async_land": False}
