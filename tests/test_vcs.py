"""Tests for src/vcs.py — the VCS abstraction (GitVCS + HgVCS backends).

Topic T-vcs-checkpoint: VCS provenance + per-task restore. Backends must be
best-effort (never raise on a bad path / non-repo) and support detect / head /
is_dirty / make_safety_branch over git and hg working copies.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import vcs as vcs_mod  # noqa: E402

HAVE_HG = shutil.which("hg") is not None


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "g"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


# ---------------------------------------------------------------------------
# detect / get_backend
# ---------------------------------------------------------------------------


def test_detect_git(git_repo):
    assert vcs_mod.detect(str(git_repo)) == "git"


def test_detect_none_for_plain_dir(tmp_path):
    assert vcs_mod.detect(str(tmp_path)) is None


def test_detect_none_for_missing_path(tmp_path):
    assert vcs_mod.detect(str(tmp_path / "nope")) is None


def test_get_backend_git_returns_gitvcs():
    assert isinstance(vcs_mod.get_backend("git"), vcs_mod.GitVCS)


def test_get_backend_unknown_returns_none():
    assert vcs_mod.get_backend("svn") is None
    assert vcs_mod.get_backend(None) is None


# ---------------------------------------------------------------------------
# GitVCS
# ---------------------------------------------------------------------------


def test_git_head_matches_rev_parse(git_repo):
    expected = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    assert vcs_mod.GitVCS().head(str(git_repo)) == expected


def test_git_is_dirty_clean_then_dirty(git_repo):
    g = vcs_mod.GitVCS()
    assert g.is_dirty(str(git_repo)) is False
    (git_repo / "a.txt").write_text("changed\n")
    assert g.is_dirty(str(git_repo)) is True


def test_git_make_safety_branch_creates_and_switches(git_repo):
    g = vcs_mod.GitVCS()
    sha = g.head(str(git_repo))
    # advance HEAD so the branch must point at the OLD sha
    (git_repo / "b.txt").write_text("two\n")
    _git(git_repo, "add", "b.txt")
    _git(git_repo, "commit", "-q", "-m", "second")
    ok = g.make_safety_branch(str(git_repo), sha, "juggle/pre-test")
    assert ok is True
    cur = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert cur == "juggle/pre-test"
    assert g.head(str(git_repo)) == sha


def test_git_head_none_on_non_repo(tmp_path):
    assert vcs_mod.GitVCS().head(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# HgVCS (skipped if hg not installed)
# ---------------------------------------------------------------------------


@pytest.fixture
def hg_repo(tmp_path):
    repo = tmp_path / "h"
    repo.mkdir()
    subprocess.run(["hg", "init"], cwd=repo, check=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["hg", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(
        ["hg", "commit", "-m", "first", "-u", "t"], cwd=repo, check=True
    )
    return repo


@pytest.mark.skipif(not HAVE_HG, reason="hg not installed")
def test_detect_hg(hg_repo):
    assert vcs_mod.detect(str(hg_repo)) == "hg"


@pytest.mark.skipif(not HAVE_HG, reason="hg not installed")
def test_hg_head_and_dirty(hg_repo):
    h = vcs_mod.get_backend("hg")
    assert h.head(str(hg_repo))
    assert h.is_dirty(str(hg_repo)) is False
    (hg_repo / "a.txt").write_text("changed\n")
    assert h.is_dirty(str(hg_repo)) is True
