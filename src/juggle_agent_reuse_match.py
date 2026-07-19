"""juggle_agent_reuse_match — pure idle-agent reuse candidate predicate.

Extracted from juggle_dispatch_core._reuse_idle_agent (LOC-gate budget) so the
poisoned-model checkout guard (bug KB, 2026-07-19) has room to land. Pure, no
DB/I/O.
"""
from __future__ import annotations


def candidate_matches(candidate: dict, *, role, target_repo, requested_harness,
                      model_match) -> bool:
    """True if ``candidate`` structurally matches a reuse request: repo/role/
    harness, plus the model preference (model-blind when ``model_match`` is
    None — see _reuse_idle_agent's docstring for the headroom-preference pass)."""
    agent_repo = candidate.get("repo_path")
    if agent_repo is None:
        return False
    if target_repo and agent_repo != target_repo:
        return False
    if role and candidate.get("role") != role:
        return False
    if candidate.get("harness") != requested_harness:
        return False
    if model_match is not None and candidate.get("model") != model_match:
        return False
    return True
