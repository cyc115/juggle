"""vcs — a tiny VCS abstraction for ledger provenance + per-task restore.

Owns: detecting the VCS of a repo path and reading/manipulating its state
(HEAD sha, dirty flag, safety-branch checkout) for git and Mercurial. Every
method is BEST-EFFORT: a missing path, a non-repo, or a tool error yields
``None`` / ``False`` rather than raising, because these run inside the
dispatch/completion choke points which must never break.

Must not own: ledger SQL (dbops.runs), CLI glue (juggle_cmd_runs), or any
juggle-specific naming conventions beyond what the caller passes in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable


def _run(args: list[str], cwd: str) -> str | None:
    """Run a command, returning stripped stdout, or None on any failure."""
    try:
        r = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


@runtime_checkable
class VCS(Protocol):
    """A version-control backend. All methods are best-effort."""

    def head(self, path: str) -> str | None: ...
    def is_dirty(self, path: str) -> bool: ...
    def make_safety_branch(self, path: str, sha: str, name: str) -> bool: ...


class GitVCS:
    """git backend: rev-parse / status --porcelain / branch + switch."""

    def head(self, path: str) -> str | None:
        return _run(["git", "rev-parse", "HEAD"], path) or None

    def is_dirty(self, path: str) -> bool:
        out = _run(["git", "status", "--porcelain"], path)
        return bool(out)

    def make_safety_branch(self, path: str, sha: str, name: str) -> bool:
        if _run(["git", "branch", name, sha], path) is None:
            return False
        return _run(["git", "switch", name], path) is not None


class HgVCS:
    """Mercurial backend: hg id -i / hg status / hg update -r + bookmark."""

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
    """Return 'git' | 'hg' | None for the repo rooted at (or above) ``path``."""
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
