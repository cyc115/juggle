"""juggle_prompt_context — PromptContext + render_agent_prompt (Agent Prompt
Contract v2, PC1 + pc3-emitters-sweep:
docs/2026-07-03-agent-prompt-contract-v2-spec.md).

Owns: the PromptContext/TaskSpec/VcsInfo/RepoProfile dataclasses (every field
concrete — no dispatch-time templating), ``vcs_info_for`` (thin adapter over
vcs.backend_for with a generic fallback when no backend is detectable),
``render_finalize_command``/``render_fail_command`` (the single-spelling
finalize/fail command literals — every emitter that needs one, in this
module or elsewhere, calls these instead of building the string itself), and
``render_agent_prompt(ctx, role)`` — the pure renderer entrypoint that
assembles TASK -> LIFECYCLE -> GUARDRAILS in that strict order (role in
coder/planner/researcher; section bodies live in juggle_prompt_sections,
split out under the LOC gate). A rendered prompt has ZERO unbound
``<...>``/``{...}`` tokens (the finalize command's own
``<summary>``/``<key findings>``/``<error>`` argument placeholders are the
one sanctioned exception — they are filled in by the AGENT, never by this
renderer) and states the finalize/full-suite/quality-gate/version-bump rules
exactly once (spec problem #6: 2-4x duplication caused the 3-spelling defect).

Must not own: repo profile RESOLUTION from CLAUDE.md/DB/defaults
(juggle_repo_profile.resolve_repo_profile), VCS backend detection beyond this
thin adapter (vcs.py), section body rendering (juggle_prompt_sections), or
any call-site wiring (dispatch modules — PC2/PC3).
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
    """Pure renderer: TASK -> LIFECYCLE -> GUARDRAILS, strict order (spec).
    Section bodies live in juggle_prompt_sections (LOC gate, pc3-emitters-
    sweep) — imported lazily to avoid a module-load cycle (that module
    imports render_finalize_command/render_fail_command from here)."""
    if role not in _SUPPORTED_ROLES:
        raise ValueError(
            f"render_agent_prompt: unsupported role {role!r} "
            f"(supported: {sorted(_SUPPORTED_ROLES)})"
        )
    import juggle_prompt_sections as _sections

    lifecycle = {
        "coder": _sections.render_lifecycle_section_coder,
        "planner": _sections.render_lifecycle_section_planner,
        "researcher": _sections.render_lifecycle_section_researcher,
    }[role](ctx)
    sections = [
        _sections.render_header(ctx, role),
        _sections.render_task_section(ctx),
        lifecycle,
        _sections.render_guardrails_section(ctx, role),
    ]
    return "\n\n".join(sections) + "\n"
