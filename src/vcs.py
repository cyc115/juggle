"""vcs — the VCS backend seam for all of juggle's repo operations.

Owns: the intent-level ``VCS`` Protocol (identity/detection, read-only history
facts, workspace lifecycle, publish — 15 methods, ``VCS_PROTOCOL_VERSION``),
the in-tree ``GitVCS``/``HgVCS`` backends, ``detect()``/``get_backend()``, and
``backend_for(repo)`` — the single resolution funnel every widened call site
routes through (repo-config override → builtin detect → plugin scan seam →
per-``primary_root`` cache).

Every method is BEST-EFFORT unless noted: a missing path, a non-repo, or a
tool error yields ``None``/``False``/``""``/``[]`` rather than raising, because
these run inside dispatch/integrate/watchdog choke points which must never
break on a bad path. ``update_to`` and ``submit`` are the two mutators that can
fail meaningfully — they return rich result objects so callers can fail loud.

Must not own: ledger SQL (dbops.runs), CLI glue (juggle_cmd_runs), orchestrator
policy (locks, dirty gates, test runs, action-item filing — stays in the
calling pipeline), or juggle-specific naming conventions (``cyc_<label>``,
``juggle-<basename>-<label>``) — callers compute those and pass them in.

See docs/create-your-own-vcs-backend.md for the out-of-tree plugin contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from vcs_git import GitVCS
from vcs_types import (  # noqa: F401 — re-exported for callers
    Capabilities,
    LandStatus,
    Rev,
    SubmitMode,
    SubmitResult,
    UpdateResult,
    WorkspaceResult,
    _run,
)

# Bumps on ANY Protocol change: new/removed/renamed method, or changed
# argument/return semantics (§7.7). An out-of-tree plugin whose PROTOCOL_VERSION
# doesn't match is rejected by the (future) plugin loader rather than silently
# missing a method on a hot path.
VCS_PROTOCOL_VERSION = 1


@runtime_checkable
class VCS(Protocol):
    """A version-control backend. Every method is best-effort unless noted."""

    name: str
    capabilities: Capabilities

    # ── identity & detection ────────────────────────────────────────────────
    def repo_root(self, path: str) -> str | None: ...
    def primary_root(self, repo: str) -> str: ...

    # ── history facts (read-only, repo-scoped) ──────────────────────────────
    def refresh(self, repo: str) -> None: ...
    def trunk(self, repo: str) -> Rev | None: ...
    def resolve(self, repo: str, ref: str | None = None) -> Rev | None: ...
    def is_ancestor(self, repo: str, rev: str, of: str) -> bool: ...

    # ── workspace lifecycle ──────────────────────────────────────────────────
    def create_workspace(
        self, repo: str, name: str, root: str, *, base: Rev | None = None
    ) -> WorkspaceResult: ...
    def remove_workspace(self, repo: str, ws: str) -> bool: ...
    def current_rev(self, ws: str) -> Rev | None: ...
    def dirty_files(self, ws: str) -> list[str]: ...
    def has_changes(self, ws: str, *, since: Rev) -> bool: ...
    def update_to(self, ws: str, base: Rev) -> UpdateResult: ...
    def describe_changes(self, ws: str, *, since: Rev) -> str: ...

    # ── publish ──────────────────────────────────────────────────────────────
    def submit(
        self, ws: str, *, base: Rev, mode: SubmitMode, push: bool = True
    ) -> SubmitResult: ...
    def land_status(self, repo: str, ticket: str) -> LandStatus: ...

    # ── back-compat aliases (runs-ledger consumers) ─────────────────────────
    def head(self, path: str) -> str | None: ...
    def is_dirty(self, path: str) -> bool: ...
    def make_safety_branch(self, path: str, sha: str, name: str) -> bool: ...


class HgVCS:
    """Mercurial backend precedent: only the pre-widening ledger surface
    (head/is_dirty/make_safety_branch). Not a full VCS Protocol implementation
    — Sapling/Mercurial ship as an out-of-tree plugin (§2.5), never as a new
    in-tree arm; this stub is kept only for the existing runs-ledger callers."""

    name = "hg"

    def head(self, path: str) -> str | None:
        # `hg id -i` prints the working-dir parent sha (trailing '+' if dirty).
        out = _run(["hg", "id", "-i"], path)
        return out.rstrip("+") if out else None

    def is_dirty(self, path: str) -> bool:
        out = _run(["hg", "status"], path)
        return bool(out)

    def make_safety_branch(self, path: str, sha: str, name: str) -> bool:
        if _run(["hg", "update", "-r", sha], path) is None:
            return False
        return _run(["hg", "bookmark", name], path) is not None


def detect(path: str) -> str | None:
    """Return 'git' | 'hg' | None for the repo rooted at (or above) ``path``.

    Builtin detection only (git + the hg stub) — Sapling detection is NOT
    builtin, it comes from an out-of-tree plugin's optional ``detect()``
    (§2.5), scanned only after these builtins miss. Git-first precedence is
    intentional: a repo with both ``.git`` and ``.sl`` is treated as git.
    """
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    if (p / ".git").exists() or _run(
        ["git", "rev-parse", "--is-inside-work-tree"], path
    ) == "true":
        return "git"
    if (p / ".hg").exists() or _run(["hg", "root"], path):
        return "hg"
    return None


def get_backend(vcs_type: str | None) -> VCS | None:
    """Return a backend instance for a vcs_type, or None if unsupported."""
    if vcs_type == "git":
        return GitVCS()
    if vcs_type == "hg":
        return HgVCS()
    return None


_PLUGIN_CACHE: dict[str, tuple[VCS, object]] | None = None


def _scanned_plugins() -> dict[str, tuple[VCS, object]]:
    """Scan ``~/.juggle/vcs_plugins/*.py`` once per process (§2.5.1). Cached
    for the process lifetime — same lifecycle as ``_BACKEND_CACHE``."""
    global _PLUGIN_CACHE
    if _PLUGIN_CACHE is None:
        from vcs_plugins import load_plugins

        _PLUGIN_CACHE = load_plugins(VCS_PROTOCOL_VERSION)
    return _PLUGIN_CACHE


def _plugin_backend_by_name(name: str) -> VCS | None:
    """Config-bound resolution: ``vcs: "<name>"`` binds directly to the
    plugin whose ``BACKEND.name`` matches — skips ``detect()`` entirely."""
    entry = _scanned_plugins().get(name)
    return entry[0] if entry else None


def _plugin_backend_for(repo: str) -> VCS | None:
    """Seam for out-of-tree VCS plugin resolution (§2.5) — scanned after the
    builtins miss. Consults each loaded plugin's optional ``detect()``."""
    for backend, detect_fn in _scanned_plugins().values():
        if detect_fn is not None and detect_fn(repo):
            return backend
    return None


_BACKEND_CACHE: dict[str, VCS] = {}


def backend_for(repo: str) -> VCS:
    """The single resolution funnel every widened call site routes through.

    Order: repo-config ``vcs`` override → builtin ``detect()`` (git/hg) →
    plugin ``detect()`` scan (seam, §2.5) → cache per resolved ``primary_root``
    for the process lifetime. Fail-loud (raises) rather than silently falling
    back — a misconfigured/undetectable repo must never silently bind to the
    wrong backend.
    """
    from juggle_settings import get_repo_config

    override = get_repo_config(repo).get("vcs")
    if override:
        backend = get_backend(override) or _plugin_backend_by_name(override)
        if backend is None:
            raise ValueError(f"unknown vcs backend '{override}' configured for repo {repo}")
    else:
        vcs_type = detect(repo)
        backend = get_backend(vcs_type) if vcs_type else _plugin_backend_for(repo)
        if backend is None:
            raise ValueError(f"cannot detect a VCS backend for repo {repo}")

    root = backend.primary_root(repo) or str(Path(repo).resolve())
    cached = _BACKEND_CACHE.get(root)
    if cached is not None:
        return cached
    _BACKEND_CACHE[root] = backend
    return backend
