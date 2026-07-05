"""juggle_dispatch_core — Internal dispatch primitive shared by the watchdog tick
and the user-facing CLI commands.

Owns: acquire_agent (pool walk + CAS-assign + thread→background),
      send_task_to_agent (pane verify, worktree, prompt build, tmux send, ledger),
      dispatch_node (compose both — used by graph_tick via _dispatch_via_pool).
Must not own: armed-project logic, tick orchestration, CLI arg parsing, sys.exit.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import juggle_cmd_agents_common as _com
from juggle_dispatch_literal import (  # noqa: F401 — re-exported for tests
    literalize_finalize_commands,
)
from juggle_graph_dispatch import TASK_ROLE, CapacityError

_log = logging.getLogger("juggle-dispatch-core")
# Sole production default for auto-created worktree roots (2026-06-20 leak fix).
DEFAULT_WORKTREE_ROOT = os.environ.get("JUGGLE_WORKTREE_ROOT", "/tmp")


def _reuse_idle_agent(
    db, mgr, thread_id: str, *, role, target_repo, requested_harness, model_match,
) -> dict | None:
    """CAS-claim the first idle agent matching repo/role/harness (warm reuse:
    /clear + cd), or None. ``model_match`` None → model-blind (today's behavior);
    non-NULL → constrain to a pane whose IMMUTABLE launch model equals it (the
    headroom-preference pass). Claiming never grows the pool, so the cap is the
    caller's spawn-branch job (2026-07-01 reuse-before-cap incident)."""
    for candidate in db.get_ranked_idle_agents(thread_id, role=role):
        agent_repo = candidate.get("repo_path")
        if agent_repo is None:
            continue
        if target_repo and agent_repo != target_repo:
            continue
        if role and candidate.get("role") != role:
            continue
        if candidate.get("harness") != requested_harness:
            continue
        if model_match is not None and candidate.get("model") != model_match:
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


def send_task_to_agent(
    db,
    agent_id: str,
    thread_id: str | None,
    prompt: str,
    *,
    skip_template: bool = False,
    allow_main: bool = False,
    worktree_path_override: str | None = None,
    worktree_branch_override: str | None = None,
    main_repo_override: str | None = None,
    db_path: str | None = None,
    prompt_version: str | None = None,
    _mgr=None,
) -> None:
    """Pane verify, worktree auto-create, prompt build (template + preamble),
    tmux send, agent-field update, ledger insert.

    Raises RuntimeError on hard failure. Does NOT release the agent on error —
    caller (dispatch_node) handles cleanup. Never calls sys.exit.
    The check_task_guard lives at the CLI layer (cmd_send_task); the tick path
    (dispatch_node) skips it by calling this function directly.
    """
    agent = db.get_agent(agent_id)
    if agent is None:
        raise RuntimeError(f"agent {agent_id} not found")

    mgr = _mgr or _com.JuggleTmuxManager()
    _role = agent.get("role")
    _dispatch_cfg = _com._get_settings().get("agent", {})
    _agent_harness = agent.get("harness") or _dispatch_cfg.get("harness") or "claude"
    adapter = _com.get_adapter(_role, agent_cfg=dict(_dispatch_cfg, harness=_agent_harness))

    pane_id = agent["pane_id"]
    if not mgr.verify_pane(pane_id):
        mgr.ensure_session()
        new_pane_id = mgr.spawn_pane()
        mgr.start_agent_in_pane(new_pane_id, role=_role)
        db.update_agent(agent_id, pane_id=new_pane_id)
        pane_id = new_pane_id
        agent = db.get_agent(agent_id)
        is_new = True
    else:
        is_new = False

    # Worktree auto-create (coder/planner only) + '## Working Directory' section.
    from juggle_dispatch_worktree_context import build_worktree_context
    _worktree_context = build_worktree_context(
        db, role=_role, agent=agent, pane_id=pane_id, thread_id=thread_id,
        allow_main=allow_main, worktree_path_override=worktree_path_override,
        worktree_branch_override=worktree_branch_override,
        main_repo_override=main_repo_override,
        default_worktree_root=DEFAULT_WORKTREE_ROOT,
    )

    # '## Source of truth' section (T-fix-dispatch-plan-spec-provision):
    # immediately after Working Directory, before the role template, so it's
    # the first thing a coder reads about WHAT to build.
    from juggle_graph_hydration import topic_source_of_truth_for_thread
    _source_of_truth = topic_source_of_truth_for_thread(db, thread_id)

    # Literal finalize rendering (T-fix-literal-finalize-line): resolve the
    # thread label so finalize/mark commands carry it baked in.
    thread_wt = db.get_thread(thread_id) if thread_id else None
    thread_label = (
        (thread_wt.get("user_label") or thread_wt["id"][:6])
        if thread_wt else (thread_id or "<thread-unresolved>")
    )

    # Prompt build. coder/planner/researcher: render TASK/LIFECYCLE/GUARDRAILS
    # once via render_agent_prompt (PC2 + PC3, Agent Prompt Contract v2) — no
    # separate UNIVERSAL_PREAMBLE/TASK_TEMPLATES/literalize_finalize_commands
    # pass. ``--no-template`` (skip_template) is the sole remaining caller of
    # the legacy path — an explicit opt-out, not an unswept role.
    if not skip_template and _role in ("coder", "planner", "researcher"):
        from juggle_dispatch_agent_render import render_agent_dispatch_prompt

        full_prompt = _worktree_context + _source_of_truth + render_agent_dispatch_prompt(
            prompt, role=_role, thread_wt=thread_wt, agent=agent, thread_label=thread_label,
        )
    else:
        if not skip_template and _role:
            templates = _com._get_settings().get("task_templates", {})
            template = templates.get(_role, "")
            if template:
                qg = _com._get_settings()["agent"].get("quality_gate_skill", "mike:pre-pr")
                template = template.replace("{quality_gate_skill}", qg)
                prompt = template + "\n---\n\n" + prompt.rstrip()

        full_prompt = (
            _com.UNIVERSAL_PREAMBLE + _worktree_context + _source_of_truth + prompt.rstrip()
        )
        full_prompt = literalize_finalize_commands(full_prompt, thread_label)
    try:
        full_prompt = adapter.decorate_task(_role, full_prompt, thread_label=thread_label)
    except Exception:
        pass

    # Send
    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(agent_id, last_active=now)
    if adapter.is_interactive:
        pane_hash = mgr.send_task(pane_id, full_prompt, is_new=is_new)
        oneshot_pid = None
    else:
        pane_hash, oneshot_pid = mgr.run_task_oneshot(
            pane_id, full_prompt, role=_role, model=agent.get("model")
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    _update: dict = dict(
        last_task=full_prompt,
        last_send_task_pane_hash=pane_hash,
        last_send_task_at=now_iso,
        harness=adapter.id,
        model=adapter._cfg.get("model") or agent.get("model"),
    )
    if not adapter.is_interactive and oneshot_pid is not None:
        _update["oneshot_pid"] = oneshot_pid
    db.update_agent(agent_id, **_update)

    # Ledger (best-effort — never breaks dispatch). Extracted to
    # juggle_dispatch_ledger (2026-06-30 orchestration-metrics; LOC budget).
    from juggle_dispatch_ledger import record_dispatch_run
    record_dispatch_run(
        db, thread_id=thread_id, agent=agent, agent_id=agent_id, role=_role,
        full_prompt=full_prompt, model=_update.get("model"), harness=adapter.id,
        prompt_version=prompt_version,
    )


def dispatch_node(
    db,
    thread_id: str,
    prompt: str,
    task: dict,
    *,
    role: str = TASK_ROLE,
    model=None,
) -> None:
    """Acquire an agent and send the task. Used by the watchdog tick.

    Raises CapacityError (pool full → tick should defer) or RuntimeError.
    Cleans up the agent binding on any send failure so the slot is not leaked.
    """
    agent = acquire_agent(db, thread_id, role=role, model=model)
    try:
        send_task_to_agent(
            db, agent["id"], thread_id, prompt,
            db_path=str(db.db_path),
        )
    except BaseException as exc:
        db.update_agent(agent["id"], status="idle", assigned_thread=None)
        if isinstance(exc, SystemExit):
            raise RuntimeError(
                f"send failed for task {task['id']} (exit {exc.code})"
            ) from exc
        raise
