"""Tests for juggle_repo_binding's git-facing helpers routed through the VCS
seam (vcs-route-queries: A5/A6), plus the repo-wide grep-assert that NO raw
``subprocess.run(["git", ...])`` remains in the five touched query modules
(SPEC 2026-07-02 §6 Phase-1 exit criterion, scoped to this topic's files).
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import juggle_repo_binding as rb  # noqa: E402


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("one\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "first")
    return r


def test_git_toplevel_resolves_repo(repo):
    assert rb._git_toplevel(str(repo)) == str(repo.resolve())


def test_git_toplevel_empty_for_non_repo(tmp_path):
    assert rb._git_toplevel(str(tmp_path)) == ""


def test_git_toplevel_empty_for_empty_path():
    assert rb._git_toplevel("") == ""


def test_canonical_main_ref_resolves_local_main(repo):
    assert rb.canonical_main_ref(str(repo)) in ("main", "origin/main")


def test_canonical_main_ref_none_when_unresolvable(tmp_path):
    assert rb.canonical_main_ref(str(tmp_path / "nope")) is None


def test_main_worktree_of_resolves_repo(repo):
    assert rb.main_worktree_of(str(repo)) == str(repo.resolve())


def test_main_worktree_of_empty_for_non_repo(tmp_path):
    assert rb.main_worktree_of(str(tmp_path)) == ""


def test_main_worktree_of_from_linked_worktree(repo, tmp_path):
    ws = tmp_path / "ws"
    _git(repo, "worktree", "add", "-b", "cyc_x", str(ws))
    assert rb.main_worktree_of(str(ws)) == str(repo.resolve())


# ---------------------------------------------------------------------------
# Grep-assert: no raw `subprocess.run(["git"` remains in the routed A4/A5/A6
# modules (SPEC §6 Phase-1 exit criterion).
# ---------------------------------------------------------------------------

_ROUTED_MODULES = [
    "src/dbops/graph_guards.py",
    "src/juggle_integrate_mergedsha.py",
    "src/juggle_repo_binding.py",
    "src/juggle_watchdog_restart.py",
    "src/juggle_cmd_graph.py",
]

_RAW_GIT_CALL = re.compile(r'subprocess\.(run|check_output)\(\s*\[\s*["\']git["\']')


def test_no_raw_git_subprocess_in_routed_modules():
    offenders = []
    for rel in _ROUTED_MODULES:
        f = REPO_ROOT / rel
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if _RAW_GIT_CALL.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "raw git subprocess call still present:\n" + "\n".join(offenders)
