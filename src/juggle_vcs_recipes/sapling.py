"""~/.juggle/vcs_plugins/sapling.py — a Sapling (`sl`) + Phabricator backend
recipe: the motivating async-land case (submit a diff, land is external/async).

This is a TEMPLATE. It shells out to real `sl`/`jf`, so it can only be
conformance-verified on a machine where those tools exist (juggle's CI has
neither — see docs/create-your-own-vcs-backend.md §4). Fill in / adjust the
repo-specific commands (`jf submit`, the land-status query) for your setup,
drop it at ~/.juggle/vcs_plugins/sapling.py, bind a repo with
``"vcs": "sapling"``, then run the conformance kit against live `sl` in your
plugin repo.

Async-land shape (facts 2026-07-05 §5): ``capabilities.async_land=True``;
``submit(mode="queue")`` runs ``jf submit`` and returns
``status="submitted", ticket=<diff id>``; ``land_status`` resolves the Phab diff
to a landed commit via Sapling's mutation ``successors()``, fail-closed to
``unlanded`` on any doubt (a false "landed" is the one unrecoverable mistake).
"""
from __future__ import annotations

import subprocess

PROTOCOL_VERSION = 1


def _run(args, cwd):
    try:
        r = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


class _Caps:
    def __init__(self, async_land, auto_restack):
        self.async_land = async_land
        self.auto_restack = auto_restack


class _WorkspaceResult:
    def __init__(self, ok, path="", branch="", detail=""):
        self.ok, self.path, self.branch, self.detail = ok, path, branch, detail


class _UpdateResult:
    def __init__(self, ok, conflicts=None, detail=""):
        self.ok, self.conflicts, self.detail = ok, conflicts or [], detail


class _SubmitResult:
    def __init__(self, status, ticket="", landed_rev=None, detail=""):
        self.status, self.ticket, self.landed_rev, self.detail = (
            status, ticket, landed_rev, detail,
        )


class _LandStatus:
    def __init__(self, state, landed_rev=None, detail=""):
        self.state, self.landed_rev, self.detail = state, landed_rev, detail


class SaplingBackend:
    name = "sapling"
    capabilities = _Caps(async_land=True, auto_restack=True)

    # identity & detection
    def repo_root(self, path):
        return _run(["sl", "root"], path)

    def primary_root(self, repo):
        return _run(["sl", "root", "--shared"], repo) or repo

    # history facts
    def refresh(self, repo):
        _run(["sl", "pull"], repo)

    def trunk(self, repo):
        return _run(["sl", "log", "-r", "remote/main", "-T", "{node}"], repo)

    def resolve(self, repo, ref=None):
        target = "." if ref is None else ref
        return _run(["sl", "log", "-r", target, "-T", "{node}"], repo)

    def is_ancestor(self, repo, rev, of):
        if not repo or not rev or not of:
            return False
        out = _run(
            ["sl", "log", "-r", f"ancestor({rev},{of})", "-T", "{node}"], repo
        )
        return out == rev

    # workspace lifecycle
    def create_workspace(self, repo, name, root, *, base=None):
        args = ["sl", "clone", "--eden", repo, root]
        if _run(args, repo) is None:
            return _WorkspaceResult(ok=False, detail="sl clone failed")
        if base is not None:
            _run(["sl", "goto", base], root)
        _run(["sl", "bookmark", name], root)
        return _WorkspaceResult(ok=True, path=root, branch=name)

    def remove_workspace(self, repo, ws):
        return _run(["sl", "clean", "--all"], ws) is not None

    def current_rev(self, ws):
        return _run(["sl", "log", "-r", ".", "-T", "{node}"], ws)

    def dirty_files(self, ws):
        out = _run(["sl", "status", "--no-status"], ws)
        return out.splitlines() if out else []

    def has_changes(self, ws, *, since):
        out = _run(
            ["sl", "log", "-r", f"{since}::. - {since}", "-T", "{node}\n"], ws
        )
        return bool(out) if out is not None else True

    def update_to(self, ws, base):
        # `sl rebase` self-aborts a prior interrupted rebase via --abort first.
        _run(["sl", "rebase", "--abort"], ws)
        out = _run(["sl", "rebase", "-d", base], ws)
        if out is None:
            _run(["sl", "rebase", "--abort"], ws)
            conflicts = self.dirty_files(ws)
            return _UpdateResult(ok=False, conflicts=conflicts)
        return _UpdateResult(ok=True)

    def describe_changes(self, ws, *, since):
        return _run(["sl", "diff", "--stat", "-r", since], ws) or ""

    # publish
    def submit(self, ws, *, base, mode, push=True):
        if mode == "queue":
            out = _run(["jf", "submit"], ws)
            if out is None:
                return _SubmitResult(status="failed", detail="jf submit failed")
            return _SubmitResult(status="submitted", ticket=self._diff_id(ws) or out)
        # direct land (rare for Phabricator) — land the current diff synchronously.
        out = _run(["jf", "land"], ws)
        if out is None:
            return _SubmitResult(status="failed", detail="jf land failed")
        return _SubmitResult(status="landed", landed_rev=self.resolve(ws))

    def land_status(self, repo, ticket):
        # Resolve the submitted diff to its landed successor on trunk. Fail
        # closed to "unlanded" on any doubt (never a false "landed").
        landed = _run(
            ["sl", "log", "-r", f"successors({ticket}) & remote/main",
             "-T", "{node}"], repo,
        )
        if landed:
            return _LandStatus(state="landed", landed_rev=landed)
        return _LandStatus(state="unlanded")

    def _diff_id(self, ws):
        return _run(["sl", "log", "-r", ".", "-T", "{phabdiff}"], ws)

    # back-compat ledger aliases
    def head(self, path):
        return self.resolve(path, None)

    def is_dirty(self, path):
        return bool(self.dirty_files(path))

    def make_safety_branch(self, path, sha, name):
        if _run(["sl", "goto", sha], path) is None:
            return False
        return _run(["sl", "bookmark", name], path) is not None


BACKEND = SaplingBackend()


def detect(repo_root: str) -> bool:
    import os

    return os.path.isdir(os.path.join(repo_root, ".sl"))
