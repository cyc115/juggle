"""Tests for juggle_cmd_agents_worktree routed through the VCS seam
(vcs-route-workspace: A1/A2 — create_workspace/remove_workspace/primary_root).

Behavior-preserving: naming (``cyc_<label>``, ``juggle-<basename>-<label>``),
idempotency, .venv symlink, and trust pre-registration stay in THIS module —
only the mechanical git operations route through the seam.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_cmd_agents_worktree import (  # noqa: E402
    _create_worktree,
    _finalize_worktree,
    _main_worktree_root,
)


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "myrepo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("one\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "first")
    return r


# ── _create_worktree ─────────────────────────────────────────────────────────


def test_create_worktree_forks_at_repo_head(repo, tmp_path):
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    ok, wt_path, branch, msg = _create_worktree(str(repo), "AB", worktree_root=str(tmp_path))
    assert ok, msg
    assert branch == "cyc_AB"
    wt_head = _git(Path(wt_path), "rev-parse", "HEAD").stdout.strip()
    assert wt_head == head


def test_create_worktree_idempotent(repo, tmp_path):
    _create_worktree(str(repo), "CD", worktree_root=str(tmp_path))
    ok, wt_path, branch, msg = _create_worktree(str(repo), "CD", worktree_root=str(tmp_path))
    assert ok
    assert "already exists" in msg


def test_create_worktree_failure_on_bad_repo(tmp_path):
    ok, wt_path, branch, msg = _create_worktree(
        str(tmp_path / "nope"), "EF", worktree_root=str(tmp_path / "wt"),
    )
    assert not ok
    assert wt_path == "" and branch == ""


def test_main_worktree_root_resolves_from_linked_worktree(repo, tmp_path):
    ok, wt_path, _, _ = _create_worktree(str(repo), "GH", worktree_root=str(tmp_path))
    assert ok
    assert Path(_main_worktree_root(wt_path)).resolve() == repo.resolve()


# ── _finalize_worktree ───────────────────────────────────────────────────────


def test_finalize_worktree_merges_removes_and_deletes_branch(repo, tmp_path):
    ok, wt_path, branch, _ = _create_worktree(str(repo), "IJ", worktree_root=str(tmp_path))
    assert ok
    (Path(wt_path) / "feature.txt").write_text("feat\n")
    _git(Path(wt_path), "add", "feature.txt")
    _git(Path(wt_path), "commit", "-q", "-m", "feature")

    thread = {
        "worktree_path": wt_path, "worktree_branch": branch,
        "main_repo_path": str(repo),
    }
    success, msg = _finalize_worktree(thread)

    assert success, msg
    assert not Path(wt_path).exists()
    assert (repo / "feature.txt").exists()
    branches = _git(repo, "branch", "--list", branch).stdout
    assert branch not in branches


def test_finalize_worktree_no_metadata_is_noop():
    success, _ = _finalize_worktree({})
    assert success


def test_finalize_worktree_already_removed_worktree(tmp_path, repo):
    thread = {
        "worktree_path": str(tmp_path / "gone"),
        "worktree_branch": "ghost",
        "main_repo_path": str(repo),
    }
    success, msg = _finalize_worktree(thread)
    assert success
    assert "already removed" in msg.lower()


def test_finalize_worktree_non_ff_leaves_worktree_intact(repo, tmp_path):
    ok, wt_path, branch, _ = _create_worktree(str(repo), "KL", worktree_root=str(tmp_path))
    assert ok

    (repo / "main_change.txt").write_text("main\n")
    _git(repo, "add", "main_change.txt")
    _git(repo, "commit", "-q", "-m", "main diverged")

    (Path(wt_path) / "wt_change.txt").write_text("wt\n")
    _git(Path(wt_path), "add", "wt_change.txt")
    _git(Path(wt_path), "commit", "-q", "-m", "wt commit")

    thread = {
        "worktree_path": wt_path, "worktree_branch": branch,
        "main_repo_path": str(repo),
    }
    success, msg = _finalize_worktree(thread)

    assert not success
    assert Path(wt_path).exists()
