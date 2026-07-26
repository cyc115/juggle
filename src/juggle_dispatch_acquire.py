"""juggle_dispatch_acquire — agent-pool acquisition for dispatch.

Owns: ``_reuse_idle_agent`` (CAS-claim a matching idle pane, warm reuse:
/clear + cd) and ``acquire_agent`` (pool walk + CAS-assign or spawn, then
thread -> background).
Must not own: prompt build / tmux send / ledger (juggle_dispatch_core), tick
orchestration, CLI arg parsing, sys.exit.

EXTRACTED MECHANICALLY from juggle_dispatch_core (2026-07-25, architecture LOC
gate): dispatch_core sat at 296/300 lines and the notebook dispatch hook needed
headroom. Bodies are byte-identical to the pre-move versions; juggle_dispatch_core
re-exports both names so every existing import and test monkeypatch
(`_core.acquire_agent`) keeps working unchanged.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import juggle_cmd_agents_common as _com
from juggle_agent_reuse_match import candidate_matches
from juggle_graph_dispatch import TASK_ROLE, CapacityError
from juggle_model_registry import is_poisoned_claude_model

_log = logging.getLogger("juggle-dispatch-core")


def _reuse_idle_agent(
    db, mgr, thread_id: str, *, role, target_repo, requested_harness, model_match,
) -> dict | None:
    """CAS-claim the first idle agent matching repo/role/harness (warm reuse:
    /clear + cd), or None. ``model_match`` None → model-blind (today's behavior);
    non-NULL → constrain to a pane whose IMMUTABLE launch model equals it (the
    headroom-preference pass). Claiming never grows the pool, so the cap is the
    caller's spawn-branch job (2026-07-01 reuse-before-cap incident)."""
    for candidate in db.get_ranked_idle_agents(thread_id, role=role):
        if not candidate_matches(candidate, role=role, target_repo=target_repo,
                                 requested_harness=requested_harness, model_match=model_match):
            continue
        # Poisoned-pool guard (bug KB, 2026-07-19): never hand back legacy data.
        if is_poisoned_claude_model(candidate.get("model"), requested_harness,
                                    settings=_com._get_settings()):
            mgr.decommission_agent(db, candidate["id"])
            continue
        if not mgr.wait_for_ready_to_paste(candidate["pane_id"], attempts=1):
            continue
        if not db.cas_assign_agent(candidate["id"], thread_id):
            continue
        # Reuse == warm process + CLEAN context: drop the accumulated
        # transcript before handing the pane a new task. Harness-gated —
        # only Claude Code has '/clear'; other harnesses skip it.
        if requested_harness == "claude":
            mgr._run_tmux("send-keys", "-t", candidate["pane_id"], "/clear", "Enter")
        reset_dir = target_repo or os.path.expanduser("~")
        mgr._run_tmux("send-keys", "-t", candidate["pane_id"], f"cd {reset_dir}", "Enter")
        return candidate
    return None


def acquire_agent(
    db,
    thread_id: str,
    *,
    role: str = TASK_ROLE,
    model=None,
    repo=None,
    harness=None,
    fresh: bool = False,
    effort=None,
    _mgr=None,
) -> dict:
    """Pool walk + CAS-assign or spawn a new agent. Sets thread status=background.

    Returns the agent dict. Raises CapacityError (pool full → tick should defer)
    or RuntimeError (spawn failure → tick may retry). Never calls sys.exit.
    """
    from juggle_db import MAX_BACKGROUND_AGENTS
    from juggle_tmux import _spawn_repo_path

    mgr = _mgr or _com.JuggleTmuxManager()

    target_repo = repo
    if target_repo is None:
        target_repo = _spawn_repo_path()

    agent_cfg = _com._get_settings().get("agent", {})
    requested_harness = harness or agent_cfg.get("harness") or "claude"

    def _reuse(model_match):
        return _reuse_idle_agent(
            db, mgr, thread_id, role=role, target_repo=target_repo,
            requested_harness=requested_harness, model_match=model_match,
        )

    agent = None
    if not fresh:
        if model:
            # Headroom preference (no idle-TTL / no anti-starvation): a launch-
            # model match wins; else prefer a right-model cold-spawn while the pool
            # has headroom; else (saturated) fall back to ANY idle pane — never starve.
            agent = _reuse(model)
            if agent is None and len(db.get_all_agents()) >= MAX_BACKGROUND_AGENTS:
                agent = _reuse(None)
        else:
            agent = _reuse(None)  # NULL model = today's behavior (reuse any match)

    if agent is None:
        # Only the spawn branch grows the pool, so enforce the cap HERE — a
        # reusable idle agent must never be refused at cap (2026-07-01 incident).
        if len(db.get_all_agents()) >= MAX_BACKGROUND_AGENTS:
            raise CapacityError(
                f"agent pool full ({MAX_BACKGROUND_AGENTS} max) for thread {thread_id}"
            )
        try:
            agent = mgr.spawn_agent(
                db, role or "researcher", model=model,
                harness_override=requested_harness, effort=effort,
            )
        except (RuntimeError, ValueError) as e:
            raise RuntimeError(f"agent spawn failed: {e}") from e
        now = datetime.now(timezone.utc).isoformat()
        kw: dict = dict(
            status="busy", assigned_thread=thread_id, last_active=now, busy_since=now
        )
        if model:
            kw["model"] = model
        if repo:
            kw["repo_path"] = target_repo
        db.update_agent(agent["id"], **kw)
    else:
        # Warm reuse: never overwrite agents.model — the pane's launch model is
        # IMMUTABLE (a reused process cannot re-model) and the headroom preference
        # matches against it. Only repo_path may be (re)bound.
        if repo:
            db.update_agent(agent["id"], repo_path=target_repo)

    db.set_conversation_background(thread_id)
    return db.get_agent(agent["id"])
