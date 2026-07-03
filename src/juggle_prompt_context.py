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


_ROLE_TITLES = {"coder": "Coder", "planner": "Planner", "researcher": "Researcher"}
_ROLE_IDENTITY = {
    "coder": "Implement exactly what is specified — no more. Minimal diff.",
    "planner": "Produce plans a coder can execute without clarification.",
    "researcher": "Produce comprehensive, well-structured, cited reports. Never fabricate URLs.",
}


def render_finalize_command(cli_path: str, thread_id: str, *, open_questions: bool = False) -> str:
    """Canonical, single-spelling finalize command (spec problem #1: the
    finalize command was spelled 3 ways across prompt emitters). Every
    emitter that needs a literal finalize command — the renderer's own
    LIFECYCLE section, the AGENT ROLE anchor (juggle_context_anchor) — calls
    this instead of building the string itself."""
    if open_questions:
        return f'{cli_path} agent complete {thread_id} "<summary>" --open-questions \'<json>\''
    return f'{cli_path} agent complete {thread_id} "<summary>" --retain "<key findings>"'


def render_fail_command(cli_path: str, thread_id: str) -> str:
    return f'{cli_path} agent fail {thread_id} "<error>"'


_SUPPORTED_ROLES = {"coder", "planner", "researcher"}


def render_agent_prompt(ctx: PromptContext, role: str) -> str:
    """Pure renderer: TASK -> LIFECYCLE -> GUARDRAILS, strict order (spec)."""
    if role not in _SUPPORTED_ROLES:
        raise ValueError(
            f"render_agent_prompt: unsupported role {role!r} "
            f"(supported: {sorted(_SUPPORTED_ROLES)})"
        )
    lifecycle = {
        "coder": _render_lifecycle_section,
        "planner": _render_lifecycle_section_planner,
        "researcher": _render_lifecycle_section_researcher,
    }[role](ctx)
    sections = [
        _render_header(ctx, role),
        _render_task_section(ctx),
        lifecycle,
        _render_guardrails_section(ctx, role),
    ]
    return "\n\n".join(sections) + "\n"


def _render_header(ctx: PromptContext, role: str) -> str:
    return (
        f"## Role: {_ROLE_TITLES[role]}\n\n"
        f"{_ROLE_IDENTITY[role]}\n\n"
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

    lines += _render_finish_tail(ctx, "coder", step)
    return "\n".join(lines)


def _render_finish_tail(ctx: PromptContext, role: str, step: int) -> list[str]:
    """Finalize + failure-path + escape-hatch lines, shared by every role's
    LIFECYCLE section — the ONE place these rules are stated (spec: finalize
    spelled once, failure path stated once)."""
    lines = [
        f"{step}. Finalize: `{render_finalize_command(ctx.cli_path, ctx.thread_id, open_questions=(role == 'planner'))}`",
        "",
        "If verify stays red after honest attempts, finalize with PARTIAL or "
        "BLOCKER in the summary instead of forcing green.",
        f"If a mark-task or finalize call itself errors, immediately run "
        f"`{render_fail_command(ctx.cli_path, ctx.thread_id)}` — never silently retry.",
    ]
    if ctx.is_juggle_repo:
        lines += [
            "",
            "Escape hatch: if mark-task or finalize refuses due to a defect "
            "YOUR change fixes, rerun it via the workspace CLI "
            f"(`uv run {ctx.workspace_path}/src/juggle_cli.py ...`) and record "
            "that override in the finalize call's summary.",
        ]
    return lines


def _render_lifecycle_section_planner(ctx: PromptContext) -> str:
    lines = [
        "## Lifecycle",
        "",
        "1. Produce the plan — every step must be verifiable by an agent "
        "(deterministic command + expected output).",
        "2. Batch unresolved questions into --open-questions; do not ask "
        "interactively.",
        "3. Include a devil's-advocate section: weakest assumption per fix "
        "+ failure mode + mitigation.",
        "4. Open the plan in Obsidian after writing.",
        "",
    ]
    lines += _render_finish_tail(ctx, "planner", 5)
    return "\n".join(lines)


def _render_lifecycle_section_researcher(ctx: PromptContext) -> str:
    lines = [
        "## Lifecycle",
        "",
        "1. Cite sources with URLs and retrieval dates.",
        "2. Distinguish facts from opinions.",
        "3. Cross-reference at least 2 sources for key claims.",
        "",
    ]
    lines += _render_finish_tail(ctx, "researcher", 4)
    return "\n".join(lines)


def _render_guardrails_section(ctx: PromptContext, role: str) -> str:
    if role == "planner":
        bullets = [
            "- Write the plan file only — never implement.",
            "- No research beyond what's needed to ground the plan in real code.",
            "- Never modify AGENTS.md, CLAUDE.md, or .codegraph files.",
        ]
    elif role == "researcher":
        bullets = [
            "- Research only — no implementation, no code changes.",
            "- Stay within the research topic; no tangent deep-dives.",
            "- Never modify AGENTS.md, CLAUDE.md, or .codegraph files.",
        ]
    else:
        bullets = [
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
    return "\n".join(["## Guardrails", ""] + bullets)
