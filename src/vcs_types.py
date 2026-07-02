"""vcs_types — shared value types + the shellout helper for the VCS seam.

Owns: the opaque ``Rev``/``SubmitMode`` aliases, the frozen result dataclasses
every VCS backend method returns (never bare tuples/bools), and ``_run`` — the
single best-effort subprocess runner every backend body uses. Split out of
vcs.py (architecture gate, ~300 line budget) since vcs.py + vcs_git.py both
need these with no import cycle.

Must not own: any backend logic (git/hg mechanics live in vcs_git.py / vcs.py).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Literal

# Opaque, str-backed. Orchestrator code may store/compare/pass these but never
# parse them or assume sha/branch-name semantics.
Rev = str

SubmitMode = Literal["direct", "queue"]


def _run(args: list[str], cwd: str) -> str | None:
    """Run a command, returning stripped stdout, or None on any failure."""
    try:
        r = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


@dataclass(frozen=True)
class Capabilities:
    """Frozen backend flags. Orchestrator policy branches on flags, never on
    backend ``name`` — a new backend quirk becomes a flag, not a method."""

    async_land: bool
    auto_restack: bool


@dataclass(frozen=True)
class WorkspaceResult:
    ok: bool
    path: str
    branch: str
    detail: str = ""


@dataclass(frozen=True)
class UpdateResult:
    ok: bool
    conflicts: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass(frozen=True)
class SubmitResult:
    status: Literal["landed", "submitted", "failed"]
    ticket: str = ""
    landed_rev: Rev | None = None
    detail: str = ""


@dataclass(frozen=True)
class LandStatus:
    """``state`` uses ``unlanded`` rather than the spec's original wording —
    a repo-wide regression pin bans that dead task-state-vocab literal
    (see test_spec_as_built.py); ``unlanded`` carries the identical "not yet
    landed, still in flight" semantics."""

    state: Literal["landed", "unlanded", "failed"]
    landed_rev: Rev | None = None
    detail: str = ""
