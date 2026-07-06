"""juggle_vcs_harness — runtime conformance harnesses for the backends juggle
ships a driver for (git builtin + the bundled toy recipe).

The shared ``vcs_conformance`` kit is generic over a duck-typed harness that
knows how to stand up throwaway repos and drive commits/dirt/trunk-advances for
ITS backend (docs/create-your-own-vcs-backend.md §4). juggle can only supply
that harness for backends it fully understands: git and toy. ``juggle vcs
conformance --backend git|toy`` runs the kit against these; every other backend
degrades to a shape-check (see juggle_vcs_validate) because only the plugin's
own repo can drive its real VCS.

Must not own: the kit itself (vcs_conformance.py) or the readiness/report logic
(juggle_vcs_validate.py).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from vcs_git import GitVCS
import juggle_vcs_recipes.toy as toy


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check
    )


class GitHarness:
    """Real git tmp repos — the actual behavioral conformance check for git."""

    def __init__(self, tmp_path):
        self.vcs = GitVCS()
        self._tmp_path = Path(tmp_path)
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


class ToyHarness:
    """Drives the pure-Python toy recipe — no external VCS binary at all.

    Commit/trunk-advance are TEST-driver operations (juggle never calls them —
    real users use their VCS's CLI), so they live here rather than in the plugin
    file, reaching into the toy store via the recipe's module-level helpers."""

    def __init__(self, tmp_path):
        self.vcs = toy.BACKEND
        self._tmp_path = Path(tmp_path)
        self._n = 0

    def new_repo(self):
        self._n += 1
        r = self._tmp_path / f"repo{self._n}"
        (r / toy._STORE).mkdir(parents=True)
        store = {"seq": 0, "trunk": None, "commits": {}}
        store["trunk"] = toy._new_rev(store, None, {"base.txt": "base\n"})
        toy._write_json(r / toy._STORE / toy._STORE_FILE, store)
        return str(r)

    def new_workspace_path(self):
        self._n += 1
        return str(self._tmp_path / f"ws{self._n}")

    def commit(self, ws, name, content):
        (Path(ws) / name).write_text(content)
        meta = toy._read_json(Path(ws) / toy._WS_META)
        store = toy._load_store(ws)
        root = toy._repo_root_of(ws)
        meta["head"] = toy._new_rev(store, meta["head"], toy._working_files(Path(ws)))
        toy._write_json(Path(ws) / toy._WS_META, meta)
        toy._save_store(root, store)

    def dirty(self, ws, name, content):
        (Path(ws) / name).write_text(content)

    def advance_trunk(self, repo, name, content):
        store = toy._load_store(repo)
        root = toy._repo_root_of(repo)
        snap = dict(store["commits"][store["trunk"]]["snapshot"])
        snap[name] = content
        store["trunk"] = toy._new_rev(store, store["trunk"], snap)
        toy._save_store(root, store)
        return store["trunk"]

    def conflicting_advance_trunk(self, repo, name, content):
        return self.advance_trunk(repo, name, content)

    def leave_interrupted_update(self, ws, new_base):
        # Simulate a half-applied update: drop the marker + leave stray dirt.
        (Path(ws) / toy._UPDATING).write_text("{}")
        (Path(ws) / "half-merged.txt").write_text("<<< partial\n")


HARNESSES = {"git": GitHarness, "toy": ToyHarness}
