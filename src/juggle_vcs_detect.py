"""juggle_vcs_detect — step 1 of ``/juggle:vcs-backend-init``: inspect a repo and
report which VCS backend + landing workflow juggle should bind.

Pure detection, zero side effects (no config writes, no scaffolding). The
downstream steps (scaffold/configure/validate) consume :class:`DetectResult`.

Recognition is marker-first (a ``.git``/``.sl``/``.hg``/``.jj``/``.toyvcs`` dir),
falling back to ``agentic`` — the seeded-coder-agent path — for a VCS with no
bundled recipe. Git is the in-tree builtin (``recipe is None`` — nothing to
scaffold); sapling/toy map to bundled recipes; everything else is agentic.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DetectResult:
    """What step 1 learned about a repo. ``recipe`` selects the resolve tier:
    ``None`` = git builtin (no plugin), a name = bundled recipe to scaffold,
    ``"agentic"`` = unknown VCS → seeded-agent fallback."""

    vcs: str | None
    trunk: str
    workflow: str  # "sync" (push-to-trunk) | "async" (submit-for-review)
    async_land: bool
    recipe: str | None


# marker-dir -> (vcs name, workflow, async_land, recipe). Order is intentional:
# git first (a repo with both .git and .sl is treated as git, matching
# vcs.detect's git-first precedence).
_MARKERS: list[tuple[str, str, str, bool, str | None]] = [
    (".git", "git", "sync", False, None),
    (".sl", "sapling", "async", True, "sapling"),
    # hg's in-tree backend is a LEDGER-ONLY stub (head/is_dirty/make_safety_branch),
    # not a full 15-method backend — an hg repo needs a real plugin, so route it to
    # the agentic fallback rather than falsely reporting a "builtin ready" repo.
    (".hg", "hg", "sync", False, "agentic"),
    (".jj", "jj", "async", True, "agentic"),
    (".toyvcs", "toy", "sync", False, "toy"),
]


def _git_trunk(repo: str) -> str:
    """Best-effort trunk BRANCH NAME (not a rev) for a git repo: the
    ``origin/HEAD`` target if set, else ``main``/``master`` if either exists,
    else the current branch. Defaults to ``main`` (juggle's git convention)."""
    origin_head = _run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo
    )
    if origin_head:
        return origin_head.split("/")[-1]
    for cand in ("main", "master"):
        if _run(["git", "rev-parse", "--verify", "--quiet", cand], repo) is not None:
            return cand
    current = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)
    return current or "main"


def _run(args: list[str], cwd: str) -> str | None:
    try:
        r = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def detect_repo(repo_path: str) -> DetectResult:
    """Detect the VCS/workflow/trunk for the repo rooted at ``repo_path``.

    Never raises: an unrecognised or non-existent path returns the agentic
    fallback (``vcs=None, recipe="agentic"``) so callers fail loud with a clear
    message rather than on a stack trace."""
    root = Path(repo_path)
    for marker, vcs, workflow, async_land, recipe in _MARKERS:
        if (root / marker).exists():
            trunk = _git_trunk(repo_path) if vcs == "git" else _default_trunk(vcs)
            return DetectResult(
                vcs=vcs,
                trunk=trunk,
                workflow=workflow,
                async_land=async_land,
                recipe=recipe,
            )
    return DetectResult(
        vcs=None, trunk="main", workflow="sync", async_land=False, recipe="agentic"
    )


def _default_trunk(vcs: str) -> str:
    """Conventional trunk ref for a non-git backend. Sapling/Phabricator repos
    canonically track ``remote/main``; the operator confirms/overrides this in
    the wizard. Anything else defaults to ``main``."""
    return "remote/main" if vcs == "sapling" else "main"
