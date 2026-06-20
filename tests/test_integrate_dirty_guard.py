"""Tests for integrate G1 (dirty-worktree), G2 (empty-branch), G3 (worktree-preservation).

Regression pin (2026-06-20): a 857-line spec was lost when integrate ran on
an uncommitted worktree — the empty branch ff-merged trivially, then worktree
cleanup deleted the uncommitted file.  G1+G2 prevent this; G3 asserts the
worktree is NEVER removed on a non-success path.
"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _git(*args, cwd):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def bare_repo(tmp_path):
    """Bare remote + local clone on main, remote tracking set up."""
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    local.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(local)], check=True, capture_output=True)
    for cmd in [
        ["git", "-C", str(local), "config", "user.email", "t@t.com"],
        ["git", "-C", str(local), "config", "user.name", "T"],
        ["git", "-C", str(local), "remote", "add", "origin", str(remote)],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    (local / "a.py").write_text("x = 1\n")
    _git("add", ".", cwd=local)
    _git("commit", "-m", "init", cwd=local)
    _git("branch", "-M", "main", cwd=local)
    subprocess.run(
        ["git", "-C", str(local), "push", "-u", "origin", "main"],
        check=True, capture_output=True,
    )
    return str(local), str(remote)


def _make_worktree(repo: str, wt_root: str, label: str) -> str:
    wt = str(Path(wt_root) / f"wt-{label}")
    subprocess.run(
        ["git", "-C", repo, "worktree", "add", "-b", f"cyc_{label}", wt],
        check=True, capture_output=True,
    )
    for cmd in [
        ["git", "-C", wt, "config", "user.email", "t@t.com"],
        ["git", "-C", wt, "config", "user.name", "T"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    return wt


def _make_db():
    db = Mock()
    db.update_thread = Mock()
    db.add_action_item = Mock()
    return db


def _thread(wt_path: str, branch: str, repo: str) -> dict:
    return {
        "id": "test-uuid",
        "worktree_path": wt_path,
        "worktree_branch": branch,
        "main_repo_path": repo,
    }


def _run(thread, db, tmp_path):
    """Call _run_integrate with mocked lock + config."""
    from juggle_cmd_integrate import _run_integrate

    lock_file = tmp_path / "test.lock"
    lock_file.write_text("")

    with (
        patch("juggle_cmd_integrate.get_repo_config",
              return_value={"push_mode": "none", "test_cmd": ""}),
        patch("juggle_cmd_integrate._assert_source_binding", return_value=None),
        patch("juggle_cmd_integrate.acquire_repo_lock", return_value=lock_file),
        patch("juggle_cmd_integrate.release_repo_lock"),
        patch("juggle_cmd_integrate._graph_task_for_thread", return_value=None),
        patch("juggle_cmd_integrate._record_merged_sha"),
        patch("juggle_cmd_integrate._restart_juggle_daemons"),
    ):
        return _run_integrate(thread, db)


# ---------------------------------------------------------------------------
# Unit tests: helper functions
# ---------------------------------------------------------------------------


def test_is_worktree_dirty_false_on_clean_tree(tmp_path, bare_repo):
    repo, _ = bare_repo
    wt = _make_worktree(repo, str(tmp_path), "clean")
    from juggle_cmd_integrate import is_worktree_dirty
    assert is_worktree_dirty(wt) is False


def test_is_worktree_dirty_true_on_untracked_file(tmp_path, bare_repo):
    repo, _ = bare_repo
    wt = _make_worktree(repo, str(tmp_path), "dirty")
    (Path(wt) / "new_file.py").write_text("work in progress\n")
    from juggle_cmd_integrate import is_worktree_dirty
    assert is_worktree_dirty(wt) is True


def test_is_worktree_dirty_true_on_modified_tracked_file(tmp_path, bare_repo):
    repo, _ = bare_repo
    wt = _make_worktree(repo, str(tmp_path), "mod")
    (Path(wt) / "a.py").write_text("x = 99\n")  # modify tracked file
    from juggle_cmd_integrate import is_worktree_dirty
    assert is_worktree_dirty(wt) is True


def test_branch_commits_ahead_zero_on_fresh_worktree(tmp_path, bare_repo):
    repo, _ = bare_repo
    _make_worktree(repo, str(tmp_path), "empty")
    from juggle_cmd_integrate import branch_commits_ahead
    assert branch_commits_ahead(repo, "cyc_empty", "origin/main") == 0


def test_branch_commits_ahead_nonzero_after_commit(tmp_path, bare_repo):
    repo, _ = bare_repo
    wt = _make_worktree(repo, str(tmp_path), "work")
    (Path(wt) / "work.py").write_text("y = 2\n")
    _git("add", "work.py", cwd=Path(wt))
    _git("commit", "-m", "add work", cwd=Path(wt))
    from juggle_cmd_integrate import branch_commits_ahead
    assert branch_commits_ahead(repo, "cyc_work", "origin/main") == 1


# ---------------------------------------------------------------------------
# G1: Dirty-worktree gate
# ---------------------------------------------------------------------------


def test_g1_dirty_worktree_refused(tmp_path, bare_repo):
    """Dirty worktree → integrate refuses; worktree still exists; nothing merged."""
    repo, _ = bare_repo
    wt = _make_worktree(repo, str(tmp_path), "dirty")
    (Path(wt) / "spec.md").write_text("my 857-line spec\n")

    db = _make_db()
    ok, msg = _run(_thread(wt, "cyc_dirty", repo), db, tmp_path)

    assert ok is False
    assert "uncommitted" in msg.lower() or "refused" in msg.lower()
    assert "spec.md" in msg or "1 file" in msg or "files" in msg.lower()


def test_g1_dirty_refused_worktree_still_exists(tmp_path, bare_repo):
    """G1 refusal must NEVER remove the worktree dir (that's the data-loss bug)."""
    repo, _ = bare_repo
    wt = _make_worktree(repo, str(tmp_path), "preserve")
    (Path(wt) / "precious.md").write_text("important data\n")

    db = _make_db()
    _run(_thread(wt, "cyc_preserve", repo), db, tmp_path)

    assert Path(wt).exists(), "Worktree must be preserved on G1 refusal"
    assert (Path(wt) / "precious.md").exists(), "Uncommitted file must be preserved"


def test_g1_names_dirty_files_in_error(tmp_path, bare_repo):
    """Error message must name the dirty files so the agent knows what to commit."""
    repo, _ = bare_repo
    wt = _make_worktree(repo, str(tmp_path), "names")
    (Path(wt) / "important.py").write_text("code\n")
    (Path(wt) / "also_important.txt").write_text("notes\n")

    db = _make_db()
    ok, msg = _run(_thread(wt, "cyc_names", repo), db, tmp_path)

    assert ok is False
    # At least one of the dirty files should appear in the error message
    assert "important.py" in msg or "also_important.txt" in msg


# ---------------------------------------------------------------------------
# G2: Empty-branch guard
# ---------------------------------------------------------------------------


def test_g2_empty_branch_clean_tree_refused(tmp_path, bare_repo):
    """0 commits ahead + clean tree → refuses 'nothing to merge'."""
    repo, _ = bare_repo
    wt = _make_worktree(repo, str(tmp_path), "empty")
    # No commits made on this branch

    db = _make_db()
    ok, msg = _run(_thread(wt, "cyc_empty", repo), db, tmp_path)

    assert ok is False
    assert "nothing to merge" in msg.lower() or "0 commits" in msg.lower()


def test_g2_empty_branch_worktree_preserved(tmp_path, bare_repo):
    """G2 refusal must preserve the worktree dir."""
    repo, _ = bare_repo
    wt = _make_worktree(repo, str(tmp_path), "g2preserve")

    db = _make_db()
    _run(_thread(wt, "cyc_g2preserve", repo), db, tmp_path)

    assert Path(wt).exists(), "Worktree must be preserved on G2 refusal"


# ---------------------------------------------------------------------------
# Happy path: real commit → merges successfully, worktree removed
# ---------------------------------------------------------------------------


def test_happy_path_real_commit_merges_and_removes_worktree(tmp_path, bare_repo):
    """A branch with a real commit should merge successfully and the worktree removed."""
    repo, _ = bare_repo
    wt = _make_worktree(repo, str(tmp_path), "happy")
    (Path(wt) / "feature.py").write_text("z = 3\n")
    _git("add", "feature.py", cwd=Path(wt))
    _git("commit", "-m", "add feature", cwd=Path(wt))

    db = _make_db()
    ok, msg = _run(_thread(wt, "cyc_happy", repo), db, tmp_path)

    assert ok is True
    assert not Path(wt).exists(), "Worktree should be removed after successful merge"
