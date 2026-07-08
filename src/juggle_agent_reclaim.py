"""juggle_agent_reclaim — reclaim ONE idle pool slot at the cap.

Owns: the `agent get` cap-recovery path — a pure LRU selector over reclaimable
idle agents (guarded against destroying unmerged worktree work) plus a thin
`reclaim_one_idle_agent(db)` that decommissions the chosen victim.

Incident (2026-07-07): `agent get --role coder` hard-failed with "pool full"
while 3 idle researchers sat unused. Reclaim one idle+unowned+clean agent (LRU)
and retry ONCE instead of refusing.

Must not own: acquisition/spawn, the watchdog dispatch path, thread CRUD.
"""

from __future__ import annotations

import sys

import juggle_cmd_agents_common as _com


def _agent_owns_unmerged_worktree(agent: dict) -> bool:
    """True iff the agent's ``repo_path`` is an isolated worktree holding commits
    not yet in trunk — reclaiming it would silently destroy unintegrated work.

    HARD safety guard (2026-07-07): a done-but-unreleased agent can still point
    at a worktree carrying unmerged commits (the L1/ST pattern). Fails SAFE — if
    we cannot PROVE the worktree is clean, treat it as unmerged (return True) so
    the agent is never destroyed.
    """
    from pathlib import Path

    from vcs import backend_for

    wp = (agent.get("repo_path") or "").strip()
    if not wp:
        return False  # no bound repo/worktree → nothing to lose
    if not Path(wp).exists():
        return False  # gone worktree → no reachable commits to protect
    try:
        backend = backend_for(wp)
        main_root = backend.primary_root(wp) or wp
        # repo_path is the MAIN repo, not an isolated worktree → not owned work.
        if Path(main_root).resolve() == Path(wp).resolve():
            return False
        trunk = backend.trunk(main_root)
        if not trunk:
            return True  # cannot compare → assume unmerged (safe)
        return backend.has_changes(wp, since=trunk)
    except Exception:
        return True  # cannot prove clean → assume unmerged (safe)


def pick_reclaimable_idle_agent(agents, has_unmerged=None):
    """Return the LRU reclaimable idle agent, or None.

    Reclaimable = ``status=='idle'`` AND empty/NULL ``assigned_thread`` AND does
    NOT own an unmerged worktree. LRU = oldest ``last_active``. Pure/DB-free:
    ``has_unmerged`` is injectable for testing (defaults to the git-backed
    ``_agent_owns_unmerged_worktree``).
    """
    if has_unmerged is None:
        has_unmerged = _agent_owns_unmerged_worktree
    candidates = [
        a
        for a in agents
        if a.get("status") == "idle"
        and not (a.get("assigned_thread") or "").strip()
        and not has_unmerged(a)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda a: a.get("last_active") or "")


def reclaim_one_idle_agent(db) -> bool:
    """Decommission exactly ONE reclaimable idle agent to free a pool slot.

    Returns True iff a victim was decommissioned. NEVER touches busy/assigned
    agents or agents holding unmerged worktree work (HARD safety guard).
    """
    victim = pick_reclaimable_idle_agent(db.get_all_agents())
    if victim is None:
        return False
    sys.path.insert(0, str(_com.SRC_DIR))
    from juggle_tmux import JuggleTmuxManager

    JuggleTmuxManager().decommission_agent(db, victim["id"])
    print(
        f"[juggle] pool at cap — decommissioned idle agent {victim['id'][:8]} "
        "to free a slot",
        file=sys.stderr,
    )
    return True
