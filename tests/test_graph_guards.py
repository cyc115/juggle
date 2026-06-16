"""Regression pins for graph_guards.branch_merged_to_main (G1).

2026-06-16: empty/unrecorded worktree_branch was fail-OPEN — topics with no
bound branch got marked 'verified' if main merely existed. Observed on
cyc_ZZ (T-graph-pane-threadid-prefix) and cyc_AA (T-topic-title-generation).
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from dbops.graph_guards import branch_merged_to_main


@pytest.fixture
def git_repo(tmp_path):
    """Minimal git repo with one commit on main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "T"],
        check=True, capture_output=True,
    )
    (repo / "f.txt").write_text("init")
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True,
    )
    return repo


# ---------------------------------------------------------------------------
# FIX 1 pins — empty/None branch must be fail-closed
# ---------------------------------------------------------------------------

def test_empty_branch_fail_closed(git_repo):
    """Empty branch returns False even when main exists.

    2026-06-16: was returning True because _git_ok(rev-parse main) succeeded.
    Topics on cyc_ZZ / cyc_AA got 'verified' while unmerged on their branch.
    """
    assert branch_merged_to_main(str(git_repo), "") is False


def test_none_branch_fail_closed(git_repo):
    """None branch returns False (fail-closed)."""
    assert branch_merged_to_main(str(git_repo), None) is False


def test_unmerged_branch_not_verified(git_repo):
    """Branch with commits NOT yet merged into main returns False."""
    repo = str(git_repo)
    subprocess.run(
        ["git", "-C", repo, "checkout", "-b", "cyc_test"],
        check=True, capture_output=True,
    )
    (git_repo / "work.txt").write_text("work")
    subprocess.run(["git", "-C", repo, "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", repo, "commit", "-m", "work"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", repo, "checkout", "main"], check=True, capture_output=True
    )

    assert branch_merged_to_main(repo, "cyc_test") is False


def test_merged_branch_verified(git_repo):
    """Branch merged into main returns True."""
    repo = str(git_repo)
    subprocess.run(
        ["git", "-C", repo, "checkout", "-b", "cyc_merged"],
        check=True, capture_output=True,
    )
    (git_repo / "merged.txt").write_text("merged")
    subprocess.run(["git", "-C", repo, "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", repo, "commit", "-m", "feat"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", repo, "checkout", "main"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", repo, "merge", "--no-ff", "cyc_merged"],
        check=True, capture_output=True,
    )

    assert branch_merged_to_main(repo, "cyc_merged") is True


def test_no_repo_fail_closed():
    """Missing/nonexistent repo returns False."""
    assert branch_merged_to_main("/nonexistent/path/repo", "cyc_foo") is False
