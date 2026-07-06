"""tests/test_vcs_cli.py — the ``juggle vcs`` group is wired into the CLI and the
init flow reports a green readiness checklist for a bundled backend, fail-loud
otherwise.

Isolation: config via _JUGGLE_CONFIG_PATH, plugins via _JUGGLE_VCS_PLUGINS_DIR,
and vcs._PLUGIN_CACHE reset — a scaffolded plugin never leaks to ~/.juggle.
"""
from __future__ import annotations

import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import vcs as vcs_mod  # noqa: E402
from juggle_cli import build_cli_parser  # noqa: E402
from juggle_cmd_vcs import cmd_vcs_detect, cmd_vcs_init  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("_JUGGLE_VCS_PLUGINS_DIR", str(tmp_path / "vcs_plugins"))
    vcs_mod._PLUGIN_CACHE = None
    vcs_mod._BACKEND_CACHE.clear()
    yield
    vcs_mod._PLUGIN_CACHE = None
    vcs_mod._BACKEND_CACHE.clear()


def _init_git(path: Path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "base.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=path, check=True)


def test_vcs_group_is_registered():
    parser = build_cli_parser()
    args = parser.parse_args(["vcs", "detect", "--repo", "/x"])
    assert args.func is cmd_vcs_detect
    assert args.repo == "/x"


def test_init_git_reports_ready(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git(repo)
    cmd_vcs_init(Namespace(repo=str(repo), recipe=None, vcs=None,
                           submit_cmd=None, land_status_cmd=None))
    out = capsys.readouterr().out
    assert "configured for this repo" in out
    assert "conformance green" in out


def test_init_toy_scaffolds_and_reports_ready(tmp_path, capsys):
    repo = tmp_path / "toyrepo"
    (repo / ".toyvcs").mkdir(parents=True)  # toy marker -> recipe=toy
    cmd_vcs_init(Namespace(repo=str(repo), recipe=None, vcs=None,
                           submit_cmd=None, land_status_cmd=None))
    out = capsys.readouterr().out
    assert "recipe scaffolded" in out
    assert "configured for this repo" in out
    # the scaffolded plugin landed in the isolated plugins dir, not ~/.juggle
    assert (tmp_path / "vcs_plugins" / "toy.py").exists()


def test_init_unknown_vcs_is_fail_loud(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    with pytest.raises(SystemExit) as ei:
        cmd_vcs_init(Namespace(repo=str(repo), recipe=None, vcs=None,
                               submit_cmd=None, land_status_cmd=None))
    assert ei.value.code == 1
