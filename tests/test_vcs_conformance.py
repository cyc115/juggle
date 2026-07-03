"""Runs the shared vcs_conformance.py kit against GitVCS (real tmp git repos)
and FakeBackend (scripted) — no Sapling in main CI (SPEC §2.5.3/§6).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from vcs_git import GitVCS  # noqa: E402
from vcs_types import SubmitResult, UpdateResult  # noqa: E402
from helpers.fake_vcs import FakeBackend  # noqa: E402

# Re-export every test_* function from the shared kit so pytest collects them
# HERE, resolving `conformance_harness` against the fixtures below.
from vcs_conformance import *  # noqa: F401,F403,E402


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check
    )


class _GitHarness:
    """Real git tmp repos — the actual behavioral conformance check."""

    def __init__(self, tmp_path):
        self.vcs = GitVCS()
        self._tmp_path = tmp_path
        self._n = 0

    def new_repo(self):
        self._n += 1
        r = self._tmp_path / f"repo{self._n}"
        r.mkdir()
        _git(r, "init", "-q", "-b", "main")
        _git(r, "config", "user.email", "t@t.t")
        _git(r, "config", "user.name", "t")
        (r / "base.txt").write_text("base\n")
        _git(r, "add", "base.txt")
        _git(r, "commit", "-q", "-m", "base")
        return str(r)

    def new_workspace_path(self):
        self._n += 1
        return str(self._tmp_path / f"ws{self._n}")

    def commit(self, ws, name, content):
        (Path(ws) / name).write_text(content)
        _git(Path(ws), "add", name)
        _git(Path(ws), "commit", "-q", "-m", f"add {name}")

    def dirty(self, ws, name, content):
        (Path(ws) / name).write_text(content)

    def advance_trunk(self, repo, name, content):
        self.commit(Path(repo), name, content)
        return self.vcs.resolve(repo)

    def conflicting_advance_trunk(self, repo, name, content):
        (Path(repo) / name).write_text(content)
        _git(Path(repo), "add", name)
        _git(Path(repo), "commit", "-q", "-m", f"conflicting {name}")
        return self.vcs.resolve(repo)

    def leave_interrupted_update(self, ws, new_base):
        _git(Path(ws), "rebase", new_base, check=False)  # leave mid-rebase


class _FakeHarness:
    """Scripted FakeBackend — verifies Protocol shape, not real git semantics."""

    def __init__(self):
        self.vcs = FakeBackend()
        self._n = 0

    def new_repo(self):
        return "/fake/repo"

    def new_workspace_path(self):
        return "/fake/ws"

    def _landed_effects(self, rev):
        self.vcs.scripted["current_rev"] = rev
        self.vcs.scripted["resolve"] = rev
        self.vcs.scripted["has_changes"] = True
        self.vcs.scripted["is_ancestor"] = True
        self.vcs.scripted["submit"] = SubmitResult(status="landed", landed_rev=rev)

    def commit(self, ws, name, content):
        self._n += 1
        self._landed_effects(f"rev-{self._n}")

    def dirty(self, ws, name, content):
        self.vcs.scripted["dirty_files"] = [name]

    def advance_trunk(self, repo, name, content):
        self._n += 1
        new_base = f"trunk-{self._n}"
        self.vcs.scripted["resolve"] = new_base
        self.vcs.scripted["is_ancestor"] = True
        self.vcs.scripted["update_to"] = UpdateResult(ok=True)
        return new_base

    def conflicting_advance_trunk(self, repo, name, content):
        new_base = self.advance_trunk(repo, name, content)
        self.vcs.scripted["update_to"] = UpdateResult(ok=False, conflicts=[name])
        return new_base

    def leave_interrupted_update(self, ws, new_base):
        pass  # no real interrupted state to simulate for a scripted double


@pytest.fixture(params=["git", "fake"])
def conformance_harness(request, tmp_path):
    if request.param == "git":
        return _GitHarness(tmp_path)
    return _FakeHarness()
