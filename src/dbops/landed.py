"""dbops.landed — the two-tier "is this branch's work on main?" oracle.

Extracted from ``dbops.graph_guards`` (2026-07-03, LOC gate) when the
integrate-wedge fix added the content-equivalence tier and pushed graph_guards
past the 300-line architecture limit. Pure git-reality predicates; re-exported
from graph_guards so the existing call/patch surface
(``graph_guards.resolve_landed_sha``) is unchanged.

Ancestry (``git merge-base --is-ancestor``, in ``graph_guards.sha_is_ancestor``)
is the exact merged? probe for an ff/true-merge, but blind to a rebased /
cherry-picked / squash landing — main then holds an *equivalent* commit under a
*different* sha, so the original branch tip is not an ancestor. ``git cherry`` /
patch-id compares diffs and recognises that landing
(research/2026-07-03-watchdog-reconciliation-patterns.md §1b). The oracle here
combines both tiers so the re-integrate driver records already-landed work
instead of RE-MERGING it.

Must not own: any state transition or DB access — it is a pure function of
(repo, branch) against the git backend.
"""

from __future__ import annotations

from pathlib import Path

from dbops.graph_guards import (
    _backend_for_fail_closed,
    resolve_branch_sha,
    sha_is_ancestor,
)


def branch_content_landed(repo: str, branch: str, *, main: str = "main") -> bool:
    """Tier-2 landed check: every commit on ``branch`` has a content-equivalent
    already on ``main`` (``git cherry`` / patch-id), i.e. the work landed via a
    rebase / cherry-pick / squash under a DIFFERENT sha. Fail-closed on a
    missing repo / branch / git error."""
    if not repo or not branch or not Path(repo).exists():
        return False
    backend = _backend_for_fail_closed(repo)
    if backend is None:
        return False
    return backend.commits_landed(repo, branch, main)


def resolve_landed_sha(repo: str, branch: str, *, main: str = "main") -> str:
    """Two-tier LANDED oracle → a REAL ancestor-of-``main`` sha proving
    ``branch``'s work is on main, or '' when it is genuinely unmerged.

    Tier 1 (ancestry, the ff/true-merge common case): if the branch tip is an
    ancestor of main, return the tip itself. Tier 2 (content-equivalence, the
    rebase/cherry-pick/squash case ancestry cannot see): if every branch commit
    has a patch-equivalent on main (``branch_content_landed``), return main's
    tip — a genuine ancestor that CONTAINS the equivalents, never the unmerged
    branch tip. This keeps verified ⟺ merged (the recorded sha is always an
    ancestor of main) while recognising a rebased landing so the re-integrate
    driver records it instead of RE-MERGING already-landed work. Fail-closed."""
    tip = resolve_branch_sha(repo, branch)
    if tip and sha_is_ancestor(repo, tip, main=main):
        return tip
    if branch_content_landed(repo, branch, main=main):
        trunk_sha = resolve_branch_sha(repo, main)
        if trunk_sha and sha_is_ancestor(repo, trunk_sha, main=main):
            return trunk_sha
    return ""
