"""juggle_prompt_context — PromptContext + render_agent_prompt (Agent Prompt
Contract v2, PC1: docs/2026-07-03-agent-prompt-contract-v2-spec.md).

Owns: the PromptContext/TaskSpec/VcsInfo/RepoProfile dataclasses (every field
concrete — no dispatch-time templating), ``vcs_info_for`` (thin adapter over
vcs.backend_for with a generic fallback when no backend is detectable), and
``render_agent_prompt(ctx, role)`` — the pure renderer that assembles
TASK -> LIFECYCLE -> GUARDRAILS in that strict order. A rendered prompt has
ZERO unbound ``<...>``/``{...}`` tokens (the finalize command's own
``<summary>``/``<key findings>``/``<error>`` argument placeholders are the
one sanctioned exception — they are filled in by the AGENT, never by this
renderer) and states the finalize/full-suite/quality-gate/version-bump rules
exactly once (spec problem #6: 2-4x duplication caused the 3-spelling defect).

Must not own: repo profile RESOLUTION from CLAUDE.md/DB/defaults
(juggle_repo_profile.resolve_repo_profile), VCS backend detection beyond this
thin adapter (vcs.py), or any call-site wiring (dispatch modules — PC2/PC3).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskSpec:
    """One task in the topic/task graph, fully resolved (no placeholders).

    ``verified``: PC2 — already-integrated task, flagged "[VERIFIED — skip]"
    in the TASK section (rendered, not silently dropped, so the agent sees
    the full task list). ``has_mark_step``: PC2 — False for a synthetic
    ad-hoc task with no graph task id to mark (juggle_dispatch_core's
    template-free ``send-task`` path); the Lifecycle section then omits the
    literal ``graph mark-task`` step, which would otherwise reference a
    nonexistent task id.
    """

    id: str
    title: str
    verify_cmd: str
    body: str
    verified: bool = False
    has_mark_step: bool = True


@dataclass(frozen=True)
class VcsInfo:
    """VCS verbs rendered from the bound backend; generic fallback when no
    backend is detectable (DA-4: plain repo, no juggle vcs config)."""

    name: str | None
    commit_verb: str
    workspace_noun: str


UNBOUND_VCS = VcsInfo(
    name=None,
    commit_verb="commit with your repository's VCS",
    workspace_noun="workspace",
)


def vcs_info_for(repo_path: str) -> VcsInfo:
    """Resolve VcsInfo for ``repo_path``; UNBOUND_VCS if no backend detects."""
    import vcs as _vcs

    try:
        backend = _vcs.backend_for(repo_path)
    except Exception:
        return UNBOUND_VCS
    if backend.name == "git":
        return VcsInfo(name="git", commit_verb="git commit", workspace_noun="branch")
    if backend.name == "hg":
        return VcsInfo(name="hg", commit_verb="hg commit", workspace_noun="bookmark")
    return VcsInfo(
        name=backend.name,
        commit_verb=f"{backend.name} commit",
        workspace_noun="workspace",
    )


@dataclass(frozen=True)
class RepoProfile:
    """Per-repo prompt profile — resolved by juggle_repo_profile, carried
    here as plain data so the renderer stays pure."""

    full_suite_cmd: str | None
    smoke_cmd: str | None
    quality_gate_skill: str | None
    version_bump_policy: str | None


@dataclass(frozen=True)
class PromptContext:
    """Everything render_agent_prompt needs. No field is ever an unbound
    placeholder — callers resolve real values before constructing this."""

    thread_id: str
    workspace_path: str
    branch: str
    repo_path: str
    cli_path: str
    tasks: tuple[TaskSpec, ...]
    plan_path: str | None
    spec_path: str | None
    vcs: VcsInfo
    profile: RepoProfile
    is_juggle_repo: bool = False
    context: str | None = None
    """PC2 — free-text narrative (project objective, upstream topic
    handoffs) rendered at the top of the TASK section, above the per-task
    loop. None for single ad-hoc dispatches with no graph context."""


@dataclass(frozen=True)
class TopicPromptPayload:
    """PC2 — carries a topic/task dispatch prompt's TASK-section content
    (narrative + resolved TaskSpecs) from the pure hydration builders
    (juggle_graph_hydration) to the dispatch-time renderer
    (juggle_dispatch_core), which fills in the thread/workspace/vcs/profile
    fields only known at send time and calls render_agent_prompt."""

    context: str | None
    tasks: tuple[TaskSpec, ...]


def render_agent_prompt(ctx: PromptContext, role: str) -> str:
    """Pure renderer: TASK -> LIFECYCLE -> GUARDRAILS, strict order (spec)."""
    if role != "coder":
        raise ValueError(
            f"render_agent_prompt: unsupported role {role!r} (PC1 covers 'coder' only)"
        )
    sections = [
        _render_header(ctx),
        _render_task_section(ctx),
        _render_lifecycle_section(ctx),
        _render_guardrails_section(ctx),
    ]
    return "\n\n".join(sections) + "\n"


def _render_header(ctx: PromptContext) -> str:
    return (
        "## Role: Coder\n\n"
        "Implement exactly what is specified — no more. Minimal diff.\n\n"
        "INVARIANT: this run ends by calling the finalize command in "
        "LIFECYCLE — never stop at the prompt to wait for guidance."
    )


def _render_task_section(ctx: PromptContext) -> str:
    lines = ["## Task", "", f"Workspace: `{ctx.workspace_path}` (branch `{ctx.branch}`)"]
    if ctx.plan_path:
        lines.append(f"Plan: {ctx.plan_path}")
    if ctx.spec_path:
        lines.append(f"Spec: {ctx.spec_path}")
    if ctx.context:
        lines += ["", ctx.context]
    if any(task.verified for task in ctx.tasks):
        lines += ["", "Tasks flagged VERIFIED: skip them."]
    for task in ctx.tasks:
        flag = " [VERIFIED — skip]" if task.verified else ""
        lines += ["", f"### {task.id} — {task.title}{flag}", task.body]
        if task.verify_cmd:
            lines += [
                "",
                f"Verify: `{task.verify_cmd}`",
                "Acceptance:",
                f"- [ ] `{task.verify_cmd}` passes",
            ]
    return "\n".join(lines)


def _render_lifecycle_section(ctx: PromptContext) -> str:
    lines = ["## Lifecycle"]
    for task in ctx.tasks:
        steps = ["Write a failing test first — confirm it FAILS before implementing.",
                  "Implement the minimum code to make it pass."]
        if task.verify_cmd:
            steps.append(f"Run its verify_cmd: `{task.verify_cmd}`")
        steps.append(
            f"Commit the unit ({ctx.vcs.commit_verb} on {ctx.vcs.workspace_noun} `{ctx.branch}`)."
        )
        if task.has_mark_step:
            steps.append(
                f"Mark: `{ctx.cli_path} graph mark-task {task.id} --handoff "
                "'<files touched, interfaces changed, key decisions>'`"
            )
        lines += ["", f"### {task.id} — {task.title}"]
        lines += [f"{i}. {step}" for i, step in enumerate(steps, start=1)]

    step = 6
    lines += ["", "### Finish (once, after the last task)"]
    if ctx.profile.full_suite_cmd:
        lines.append(f"{step}. Full-suite: `{ctx.profile.full_suite_cmd}`")
    else:
        lines.append(
            f"{step}. Full-suite: no full-suite command configured for this "
            "repo — your verify_cmds are the gate; say so in the summary."
        )
    step += 1

    if ctx.profile.quality_gate_skill:
        lines.append(f"{step}. Quality gate: run {ctx.profile.quality_gate_skill} before completing.")
    else:
        lines.append(
            f"{step}. Quality gate: no quality-gate skill configured for "
            "this repo — skip this step and say so in the summary."
        )
    step += 1

    if ctx.profile.version_bump_policy:
        lines.append(f"{step}. Version bump: per {ctx.profile.version_bump_policy}.")
        step += 1

    lines.append(
        f'{step}. Finalize: `{ctx.cli_path} agent complete {ctx.thread_id} '
        '"<summary>" --retain "<key findings>"`'
    )

    lines += [
        "",
        "If verify stays red after honest attempts, finalize with PARTIAL or "
        "BLOCKER in the summary instead of forcing green.",
        f'If a mark-task or finalize call itself errors, immediately run '
        f'`{ctx.cli_path} agent fail {ctx.thread_id} "<error>"` — never silently retry.',
    ]

    if ctx.is_juggle_repo:
        lines += [
            "",
            "Escape hatch: if mark-task or finalize refuses due to a defect "
            "YOUR change fixes, rerun it via the workspace CLI "
            f"(`uv run {ctx.workspace_path}/src/juggle_cli.py ...`) and record "
            "that override in --retain.",
        ]

    return "\n".join(lines)


def _render_guardrails_section(ctx: PromptContext) -> str:
    return "\n".join(
        [
            "## Guardrails",
            "",
            "- Scope: only touch files directly related to the task — no "
            "refactoring, cleanup, or bonus work; never modify AGENTS.md, "
            "CLAUDE.md, or .codegraph files.",
            "- Never touch the shared production DB, and never run a DB "
            "migration from agent context.",
            "- Pre-existing test failures (present on the base commit) ARE "
            "your concern — proactively fix them unless the task says "
            "otherwise, and note them in --retain.",
            "- Integration is watchdog-owned — never run the integrate "
            "command yourself.",
            f"- Commit incrementally: {ctx.vcs.commit_verb} each completed, "
            f"test-passing unit to your {ctx.vcs.workspace_noun} as you go "
            "— do not defer everything to one final commit. "
            "Committed increments survive an interrupted or crashed run; a "
            "half-baked or errored final state should not be committed.",
        ]
    )
