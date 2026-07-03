"""juggle_prompt_sections — TASK/LIFECYCLE/GUARDRAILS section builders for
render_agent_prompt (Agent Prompt Contract v2, PC1 + pc3-emitters-sweep:
docs/2026-07-03-agent-prompt-contract-v2-spec.md).

Extracted from juggle_prompt_context.py (2026-07-03, LOC gate — pc3-emitters-
sweep added planner/researcher rendering and pushed the module past its
budget). Owns: the per-role header/task/lifecycle/guardrails string builders.
Must not own: the PromptContext/TaskSpec/RepoProfile/VcsInfo dataclasses or
the render_agent_prompt entrypoint (juggle_prompt_context) — this module is
called by, never calls into, that entrypoint.
"""
from __future__ import annotations

from juggle_prompt_context import PromptContext, render_fail_command, render_finalize_command

_ROLE_TITLES = {"coder": "Coder", "planner": "Planner", "researcher": "Researcher"}
_ROLE_IDENTITY = {
    "coder": "Implement exactly what is specified — no more. Minimal diff.",
    "planner": "Produce plans a coder can execute without clarification.",
    "researcher": "Produce comprehensive, well-structured, cited reports. Never fabricate URLs.",
}


def render_header(ctx: PromptContext, role: str) -> str:
    return (
        f"## Role: {_ROLE_TITLES[role]}\n\n"
        f"{_ROLE_IDENTITY[role]}\n\n"
        "INVARIANT: this run ends by calling the finalize command in "
        "LIFECYCLE — never stop at the prompt to wait for guidance."
    )


def render_task_section(ctx: PromptContext) -> str:
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


def render_lifecycle_section_coder(ctx: PromptContext) -> str:
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


def render_lifecycle_section_planner(ctx: PromptContext) -> str:
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


def render_lifecycle_section_researcher(ctx: PromptContext) -> str:
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


def render_guardrails_section(ctx: PromptContext, role: str) -> str:
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
