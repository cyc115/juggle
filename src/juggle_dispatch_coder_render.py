"""juggle_dispatch_coder_render — coder-role dispatch prompt render (Agent
Prompt Contract v2, pc2-coder-cutover: docs/2026-07-03-agent-prompt-contract-
v2-spec.md).

Owns: ``render_coder_dispatch_prompt`` — resolves the dispatch-time fields
render_agent_prompt needs (thread label, workspace/branch, VCS, repo profile)
from the thread row + agent dict, wraps a plain-string ad-hoc prompt or a
``TopicPromptPayload`` (graph/topic dispatch) into TASK-section data, and
renders TASK -> LIFECYCLE -> GUARDRAILS once via render_agent_prompt.
Must not own: worktree creation, tmux/pane plumbing, ledger writes
(juggle_dispatch_core); repo profile resolution rules (juggle_repo_profile);
the renderer itself (juggle_prompt_context).
"""
from __future__ import annotations

from juggle_dispatch_literal import _cli_invocation_prefix
from juggle_prompt_context import (
    UNBOUND_VCS,
    PromptContext,
    RepoProfile,
    TaskSpec,
    TopicPromptPayload,
    render_agent_prompt,
    vcs_info_for,
)
from juggle_repo_profile import is_juggle_repo, resolve_repo_profile

_GENERIC_PROFILE = RepoProfile(
    full_suite_cmd=None, smoke_cmd=None, quality_gate_skill=None, version_bump_policy=None,
)


def render_coder_dispatch_prompt(prompt, *, thread_wt: dict | None, agent: dict, thread_label: str) -> str:
    """Render the full coder prompt (TASK/LIFECYCLE/GUARDRAILS) for ``prompt``.

    ``prompt`` is either a plain str (ad-hoc ``send-task`` dispatch — wrapped
    as a single mark-step-less TaskSpec) or a ``TopicPromptPayload`` (graph/
    topic dispatch — its narrative + TaskSpecs are used as-is, literal
    per-task ``graph mark-task`` commands included).
    """
    if isinstance(prompt, TopicPromptPayload):
        context = prompt.context
        tasks = prompt.tasks
    else:
        context = None
        tasks = (
            TaskSpec(
                id=thread_label, title="Task", verify_cmd="",
                body=str(prompt).rstrip(), has_mark_step=False,
            ),
        )

    repo_path = ((thread_wt or {}).get("main_repo_path") or agent.get("repo_path") or "").strip()
    workspace_path = ((thread_wt or {}).get("worktree_path") or "").strip() or repo_path
    branch = ((thread_wt or {}).get("worktree_branch") or "").strip()

    ctx = PromptContext(
        thread_id=thread_label,
        workspace_path=workspace_path,
        branch=branch,
        repo_path=repo_path,
        cli_path=_cli_invocation_prefix(),
        tasks=tasks,
        plan_path=None,
        spec_path=None,
        vcs=vcs_info_for(repo_path) if repo_path else UNBOUND_VCS,
        profile=resolve_repo_profile(repo_path) if repo_path else _GENERIC_PROFILE,
        is_juggle_repo=is_juggle_repo(repo_path) if repo_path else False,
        context=context,
    )
    return render_agent_prompt(ctx, "coder")
