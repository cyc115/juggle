"""~/.juggle/vcs_plugins/toy.py — a self-contained, dependency-free reference
VCS backend for juggle.

A REAL (if tiny) VCS in pure Python — a snapshot-DAG store on disk, NO external
binary (no git, no sl). It proves juggle's agentic/recipe path end-to-end: a
generated plugin can pass the shared ``vcs_conformance`` kit on a machine that
never installed the target VCS. Copy it as a from-scratch backend template.

State (all JSON on disk): ``<repo>/.toyvcs/store.json`` holds
``{seq, trunk: rev, commits: {rev: {parent, snapshot}}}``; ``<ws>/.toyvcs_ws.json``
holds ``{repo, branch, head, fork}``; a ``<ws>/.toyvcs_updating.json`` marker
means an interrupted update to self-recover. A commit snapshots the FULL
working-file tree (every non-``.toyvcs*`` file) — no delta encoding. The
loader's three required symbols (BACKEND / PROTOCOL_VERSION / detect) are at the
bottom; ``juggle_vcs_harness.ToyHarness`` drives it for the conformance kit.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

PROTOCOL_VERSION = 1

_STORE = ".toyvcs"
_STORE_FILE = "store.json"
_WS_META = ".toyvcs_ws.json"
_UPDATING = ".toyvcs_updating.json"
_DELETED = "\0deleted\0"  # sentinel: file removed in a change set


# ── result value types (inlined so the plugin needs ZERO juggle imports) ─────
class _Caps:
    def __init__(self, async_land: bool, auto_restack: bool):
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


# ── on-disk helpers ──────────────────────────────────────────────────────────
def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


def _repo_root_of(path: str) -> Path | None:
    """The repo dir owning ``path`` (a repo root or a workspace)."""
    p = Path(path)
    meta = _read_json(p / _WS_META)
    if meta is not None:
        return Path(meta["repo"])
    if (p / _STORE / _STORE_FILE).exists():
        return p
    return None


def _load_store(path: str) -> dict | None:
    root = _repo_root_of(path)
    if root is None:
        return None
    return _read_json(root / _STORE / _STORE_FILE)


def _save_store(root: Path, store: dict) -> None:
    _write_json(root / _STORE / _STORE_FILE, store)


def _working_files(ws: Path) -> dict[str, str]:
    """Every tracked working file (excludes ``.toyvcs*`` control files)."""
    out: dict[str, str] = {}
    for f in ws.iterdir():
        if f.is_file() and not f.name.startswith(".toyvcs"):
            out[f.name] = f.read_text()
    return out


def _materialize(ws: Path, snapshot: dict[str, str]) -> None:
    """Make the working dir exactly ``snapshot`` (removes stray tracked files)."""
    for f in ws.iterdir():
        if f.is_file() and not f.name.startswith(".toyvcs"):
            f.unlink()
    for name, content in snapshot.items():
        (ws / name).write_text(content)


def _new_rev(store: dict, parent: str | None, snapshot: dict[str, str]) -> str:
    store["seq"] = store.get("seq", 0) + 1
    rev = f"r{store['seq']}"
    store["commits"][rev] = {"parent": parent, "snapshot": snapshot}
    return rev


def _changes(base: dict, other: dict) -> dict[str, str]:
    """Files that differ between snapshots ``base`` and ``other`` (deletions ->
    the _DELETED sentinel), keyed to ``other``'s resulting content."""
    diff: dict[str, str] = {}
    for name in set(base) | set(other):
        b, o = base.get(name, _DELETED), other.get(name, _DELETED)
        if b != o:
            diff[name] = o
    return diff


# ── the backend ──────────────────────────────────────────────────────────────
class ToyBackend:
    name = "toy"
    capabilities = _Caps(async_land=False, auto_restack=False)

    # identity & detection
    def repo_root(self, path):
        root = _repo_root_of(path)
        return str(root) if root is not None else None

    def primary_root(self, repo):
        root = _repo_root_of(repo)
        return str(root) if root is not None else repo

    # history facts
    def refresh(self, repo):
        return None

    def trunk(self, repo):
        store = _load_store(repo)
        return store.get("trunk") if store else None

    def resolve(self, repo, ref=None):
        store = _load_store(repo)
        if store is None:
            return None
        if ref is None:
            meta = _read_json(Path(repo) / _WS_META)
            return meta["head"] if meta is not None else store.get("trunk")
        return ref if ref in store.get("commits", {}) else None

    def is_ancestor(self, repo, rev, of):
        if not repo or not rev or not of:
            return False
        store = _load_store(repo)
        if store is None:
            return False
        commits = store.get("commits", {})
        cur = of
        seen = set()
        while cur is not None and cur not in seen:
            if cur == rev:
                return True
            seen.add(cur)
            node = commits.get(cur)
            cur = node["parent"] if node else None
        return False

    # workspace lifecycle
    def create_workspace(self, repo, name, root, *, base=None):
        store = _load_store(repo)
        if store is None:
            return _WorkspaceResult(ok=False, detail="no toy store for repo")
        base_rev = base or store.get("trunk")
        commit = store.get("commits", {}).get(base_rev)
        if commit is None:
            return _WorkspaceResult(ok=False, detail=f"unknown base {base_rev}")
        ws = Path(root)
        ws.mkdir(parents=True, exist_ok=True)
        _materialize(ws, commit["snapshot"])
        _write_json(
            ws / _WS_META,
            {"repo": str(_repo_root_of(repo)), "branch": name,
             "head": base_rev, "fork": base_rev},
        )
        return _WorkspaceResult(ok=True, path=root, branch=name)

    def remove_workspace(self, repo, ws):
        try:
            shutil.rmtree(ws)
            return True
        except OSError:
            return False

    def current_rev(self, ws):
        meta = _read_json(Path(ws) / _WS_META)
        return meta["head"] if meta is not None else None

    def dirty_files(self, ws):
        meta = _read_json(Path(ws) / _WS_META)
        store = _load_store(ws)
        if meta is None or store is None:
            return []
        head_snap = store["commits"][meta["head"]]["snapshot"]
        return sorted(_changes(head_snap, _working_files(Path(ws))))

    def has_changes(self, ws, *, since):
        rev = self.current_rev(ws)
        if rev is None:
            return True  # conservative: assume work exists on error
        return rev != since

    def update_to(self, ws, base):
        wsp = Path(ws)
        meta = _read_json(wsp / _WS_META)
        store = _load_store(ws)
        if meta is None or store is None:
            return _UpdateResult(ok=False, detail="no workspace/store")
        root = _repo_root_of(ws)
        commits = store["commits"]
        head_snap = commits[meta["head"]]["snapshot"]
        # Self-recover from any interrupted update BEFORE retrying (idempotent).
        if (wsp / _UPDATING).exists():
            (wsp / _UPDATING).unlink()
            _materialize(wsp, head_snap)
        if base not in commits:
            return _UpdateResult(ok=False, detail=f"unknown base {base}")
        fork_snap = commits[meta["fork"]]["snapshot"]
        base_snap = commits[base]["snapshot"]
        ws_changes = _changes(fork_snap, head_snap)
        base_changes = _changes(fork_snap, base_snap)
        conflicts = sorted(
            n for n in set(ws_changes) & set(base_changes)
            if ws_changes[n] != base_changes[n]
        )
        if conflicts:
            _materialize(wsp, head_snap)  # leave the workspace clean, not mid-op
            return _UpdateResult(ok=False, conflicts=conflicts)
        merged = dict(base_snap)
        for name, val in ws_changes.items():
            if val == _DELETED:
                merged.pop(name, None)
            else:
                merged[name] = val
        new_rev = _new_rev(store, base, merged)
        meta["head"], meta["fork"] = new_rev, base
        _write_json(wsp / _WS_META, meta)
        _save_store(root, store)
        _materialize(wsp, merged)
        return _UpdateResult(ok=True)

    def describe_changes(self, ws, *, since):
        n = len(self.dirty_files(ws))
        return f"toy: {n} working change(s) since {since}"

    # publish
    def submit(self, ws, *, base, mode, push=True):
        meta = _read_json(Path(ws) / _WS_META)
        store = _load_store(ws)
        root = _repo_root_of(ws)
        if meta is None or store is None or root is None:
            return _SubmitResult(status="failed", detail="no workspace/store")
        head = meta["head"]
        if mode == "queue":
            return _SubmitResult(status="submitted", ticket=head)
        store["trunk"] = head  # direct land: trunk fast-forwards to the ws tip
        _save_store(root, store)
        return _SubmitResult(status="landed", landed_rev=head)

    def land_status(self, repo, ticket):
        if self.is_ancestor(repo, ticket, self.trunk(repo) or ""):
            return _LandStatus(state="landed", landed_rev=ticket)
        return _LandStatus(state="unlanded")

    # back-compat ledger aliases
    def head(self, path):
        return self.resolve(path, None)

    def is_dirty(self, path):
        return bool(self.dirty_files(path))

    def make_safety_branch(self, path, sha, name):
        return True


BACKEND = ToyBackend()


def detect(repo_root: str) -> bool:
    return (Path(repo_root) / _STORE).exists()
