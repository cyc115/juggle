"""tests/test_vcs_detect.py — repo VCS/workflow/trunk detection (vcs-backend-init
step 1). No external VCS needed beyond git (already a test dep): sapling/hg/toy
are recognised by their on-disk marker dir, git by a real tmp repo.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from juggle_vcs_detect import detect_repo  # noqa: E402


def _init_git(path: Path, branch: str = "main") -> None:
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "base.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=path, check=True)


def test_detect_git_repo_is_sync_builtin(tmp_path):
    _init_git(tmp_path)
    d = detect_repo(str(tmp_path))
    assert d.vcs == "git"
    assert d.trunk == "main"
    assert d.workflow == "sync"
    assert d.async_land is False
    assert d.recipe is None  # git is the in-tree builtin — no plugin to scaffold


def test_detect_git_master_trunk(tmp_path):
    _init_git(tmp_path, branch="master")
    d = detect_repo(str(tmp_path))
    assert d.vcs == "git"
    assert d.trunk == "master"


def test_detect_sapling_marker_is_async_recipe(tmp_path):
    (tmp_path / ".sl").mkdir()
    d = detect_repo(str(tmp_path))
    assert d.vcs == "sapling"
    assert d.workflow == "async"
    assert d.async_land is True
    assert d.recipe == "sapling"


def test_detect_toy_marker_maps_to_toy_recipe(tmp_path):
    (tmp_path / ".toyvcs").mkdir()
    d = detect_repo(str(tmp_path))
    assert d.vcs == "toy"
    assert d.recipe == "toy"


def test_detect_unknown_repo_falls_back_to_agentic(tmp_path):
    d = detect_repo(str(tmp_path))
    assert d.vcs is None
    assert d.recipe == "agentic"  # no bundled recipe — the seeded-agent fallback


def test_detect_hg_routes_to_agentic_not_ready(tmp_path):
    # The in-tree HgVCS is a ledger-only stub (head/is_dirty/make_safety_branch),
    # NOT a full 15-method backend — so an hg repo needs a real plugin (agentic),
    # never a "builtin ready" shape-check pass.
    (tmp_path / ".hg").mkdir()
    d = detect_repo(str(tmp_path))
    assert d.vcs == "hg"
    assert d.recipe == "agentic"
