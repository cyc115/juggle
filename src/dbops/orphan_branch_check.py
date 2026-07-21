"""dbops.orphan_branch_check — is a topic's recorded branch genuine unmerged
work, or nothing to merge?

Extracted from dbops.orphan_guard (LOC gate) so flag_unmerged_completed_topics
can gate its "run juggle integrate" escalation on git reality: a report-only
topic never records a branch at all, and a PHANTOM branch (recorded but GC'd
or never diverged from trunk) has nothing for `juggle integrate` to act on
(2026-07-20 false-escalation fix). Genuine unmerged work (branch exists, >=1
commit ahead of trunk) still escalates — bug1 protection, unchanged.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def branch_ahead_of_trunk(repo: str, branch: str) -> bool:
    """True iff ``branch`` is a LIVE ref in ``repo`` with >= 1 commit ahead of
    trunk. Reuses the same rev-list/trunk primitives the integrate path
    already relies on — never hand-rolled here."""
    from dbops.graph_guards import _repo_trunk, resolve_branch_sha
    from juggle_integrate_submit import _commit_count

    if not repo or not branch:
        return False
    if not resolve_branch_sha(repo, branch):
        return False  # branch ref missing/gone — phantom, fail-closed
    trunk = _repo_trunk(repo)
    return _commit_count(repo, trunk, branch) > 0


def branch_has_unmerged_work(repo: str, branch: str, *, node_id: str, label: str) -> bool:
    """True iff ``branch`` has genuine unmerged work worth escalating. A
    phantom branch (recorded but absent from git / 0 commits ahead of trunk)
    logs a distinct diagnostic instead of silently dropping it."""
    if branch_ahead_of_trunk(repo, branch):
        return True
    _log.info(
        "orphan_guard: topic %s [%s] branch %r in %s has nothing to merge — "
        "skipping the 'run juggle integrate' escalation",
        label, node_id, branch, repo,
    )
    return False
