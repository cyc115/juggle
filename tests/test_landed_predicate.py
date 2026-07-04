"""Two-tier LANDED predicate — ancestry + content-equivalence (git cherry).

Incident (2026-07-03 integrate-wedge RCA + watchdog-reconciliation-patterns
amendment): the re-integrate driver's trigger and ``_heal_merged_sha`` used an
ANCESTRY-only merged check (``git merge-base --is-ancestor``). Ancestry is blind
to a rebased / cherry-picked landing: main then contains an *equivalent* commit
with a *different* SHA, so the original branch tip is not an ancestor of main and
ancestry reports "not merged" even though the work IS landed. An ancestry-only
re-integrate driver would therefore RE-MERGE already-landed rebased work
(duplicate commits / spurious conflict), trading one wedge for another.

These pin the git-native content-equivalence fallback (``git cherry`` /
patch-id): a rebased-landed branch is recognised as LANDED and the driver must
NOT re-merge it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _init(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "base").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")


def _branch_unmerged(repo, branch="cyc_X"):
    """A branch with a real commit ahead of main, NEVER merged."""
    _git(repo, "checkout", "-b", branch)
    (repo / "feat").write_text("work")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat work")
    _git(repo, "checkout", "main")
    return branch


def _rebase_land(repo, branch):
    """Simulate a rebased/cherry-picked landing: main advances independently,
    then the branch's PATCH is cherry-picked onto main under a NEW sha. The
    branch tip is now NOT an ancestor of main, but its work IS on main."""
    (repo / "mainside").write_text("mainside")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "mainside")
    _git(repo, "cherry-pick", branch)


# ── VCS backend primitive: commits_landed (git cherry) ───────────────────────


def test_commits_landed_true_for_rebased_branch(tmp_path):
    from vcs_git import GitVCS

    repo = tmp_path / "r"
    _init(repo)
    branch = _branch_unmerged(repo)
    _rebase_land(repo, branch)

    b = GitVCS()
    # ancestry alone would say NOT merged …
    assert b.is_ancestor(str(repo), branch, "main") is False
    # … but content-equivalence recognises the landing.
    assert b.commits_landed(str(repo), branch, "main") is True


def test_commits_landed_false_for_genuinely_unmerged(tmp_path):
    from vcs_git import GitVCS

    repo = tmp_path / "r"
    _init(repo)
    branch = _branch_unmerged(repo)  # ahead of main, never landed

    b = GitVCS()
    assert b.is_ancestor(str(repo), branch, "main") is False
    assert b.commits_landed(str(repo), branch, "main") is False


def test_commits_landed_false_when_no_commits_ahead(tmp_path):
    from vcs_git import GitVCS

    repo = tmp_path / "r"
    _init(repo)
    # branch == main (no commits ahead): not a "landing", just nothing to do.
    b = GitVCS()
    assert b.commits_landed(str(repo), "main", "main") is False


# ── graph_guards two-tier oracle: resolve_landed_sha ─────────────────────────


def test_resolve_landed_sha_ancestry_tier(tmp_path):
    """Tier 1: an ff-merged branch (ancestor of main) → its own tip sha."""
    from dbops import graph_guards as gg

    repo = tmp_path / "r"
    _init(repo)
    _git(repo, "checkout", "-b", "cyc_X")
    (repo / "feat").write_text("work")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "work")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--ff-only", "cyc_X")  # cyc_X IS ancestor of main

    tip = gg.resolve_branch_sha(str(repo), "cyc_X")
    assert gg.resolve_landed_sha(str(repo), "cyc_X") == tip


def test_resolve_landed_sha_content_tier(tmp_path):
    """Tier 2: a rebased-landed branch (NOT an ancestor) → a real ancestor sha
    (main's tip, which contains the equivalent) so verified ⟺ merged still
    holds. Never returns the unmerged branch tip."""
    from dbops import graph_guards as gg

    repo = tmp_path / "r"
    _init(repo)
    branch = _branch_unmerged(repo)
    _rebase_land(repo, branch)

    landed = gg.resolve_landed_sha(str(repo), branch)
    assert landed, "rebased-landed branch must resolve to a landed sha"
    # The returned sha must be a real ancestor of main (verified ⟺ merged).
    assert gg.sha_is_ancestor(str(repo), landed) is True
    # It must NOT be the unmerged branch tip.
    assert landed != gg.resolve_branch_sha(str(repo), branch)


def test_resolve_landed_sha_empty_for_unmerged(tmp_path):
    """Genuinely-unmerged work resolves to '' — the driver must re-integrate it,
    never treat it as landed."""
    from dbops import graph_guards as gg

    repo = tmp_path / "r"
    _init(repo)
    branch = _branch_unmerged(repo)
    assert gg.resolve_landed_sha(str(repo), branch) == ""
