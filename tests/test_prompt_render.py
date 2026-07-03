"""Golden-prompt + hygiene pins for render_agent_prompt (Agent Prompt Contract
v2, PC1 — docs/2026-07-03-agent-prompt-contract-v2-spec.md). Two fixtures:
juggle repo w/ git backend + full profile, and a foreign repo w/ no profile
+ an undetectable ("unbound") VCS backend — the two decay paths the spec
requires the renderer to degrade EXPLICITLY on, never silently."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from juggle_prompt_context import (
    UNBOUND_VCS,
    PromptContext,
    RepoProfile,
    TaskSpec,
    VcsInfo,
    render_agent_prompt,
)

SRC = Path(__file__).parent.parent / "src"

ALLOWED_PLACEHOLDERS = {
    "<summary>",
    "<key findings>",
    "<error>",
    "<files touched, interfaces changed, key decisions>",
}


def _juggle_ctx() -> PromptContext:
    return PromptContext(
        thread_id="T-42",
        workspace_path="/tmp/juggle-juggle-XYZ",
        branch="cyc_XYZ",
        repo_path="/Users/mikechen/github/juggle",
        cli_path="juggle",
        tasks=(
            TaskSpec(
                id="pc1-prompt-context",
                title="PromptContext + repo profile + renderer seam",
                verify_cmd="uv run pytest -q tests/test_prompt_context.py tests/test_prompt_render.py",
                body="Implement PromptContext, repo profile resolution, and the render_agent_prompt seam.",
            ),
        ),
        plan_path="/Users/mikechen/Documents/personal/projects/juggle/plan/2026-07-03-agent-prompt-contract-v2.md",
        spec_path="/Users/mikechen/Documents/personal/projects/juggle/docs/2026-07-03-agent-prompt-contract-v2-spec.md",
        vcs=VcsInfo(name="git", commit_verb="git commit", workspace_noun="branch"),
        profile=RepoProfile(
            full_suite_cmd="juggle verify",
            smoke_cmd=None,
            quality_gate_skill="mike:pre-pr",
            version_bump_policy="automatic in integrate (feat/fix/breaking commit prefix) — never hand-bump",
        ),
        is_juggle_repo=True,
    )


def _foreign_ctx() -> PromptContext:
    return PromptContext(
        thread_id="T-99",
        workspace_path="/tmp/acme-widget-abcd",
        branch="feature/widget",
        repo_path="/home/dev/acme-widget",
        cli_path="uv run /opt/juggle/src/juggle_cli.py",
        tasks=(
            TaskSpec(
                id="w1",
                title="Add widget validation",
                verify_cmd="npm test",
                body="Validate widget inputs before save.",
            ),
            TaskSpec(
                id="w2",
                title="Wire validation into API",
                verify_cmd="npm run test:api",
                body="Call the validator from the API handler.",
            ),
        ),
        plan_path=None,
        spec_path=None,
        vcs=UNBOUND_VCS,
        profile=RepoProfile(
            full_suite_cmd=None,
            smoke_cmd=None,
            quality_gate_skill=None,
            version_bump_policy=None,
        ),
        is_juggle_repo=False,
    )


ALL_CTXS = [_juggle_ctx(), _foreign_ctx()]


@pytest.mark.parametrize("ctx", ALL_CTXS, ids=["juggle-repo-git", "foreign-repo-unbound-vcs"])
def test_render_has_task_lifecycle_guardrails_in_order(ctx):
    rendered = render_agent_prompt(ctx, "coder")
    task_pos = rendered.index("## Task")
    lifecycle_pos = rendered.index("## Lifecycle")
    guardrails_pos = rendered.index("## Guardrails")
    assert task_pos < lifecycle_pos < guardrails_pos


@pytest.mark.parametrize("ctx", ALL_CTXS, ids=["juggle-repo-git", "foreign-repo-unbound-vcs"])
def test_finalize_command_rendered_exactly_once_no_legacy_spellings(ctx):
    rendered = render_agent_prompt(ctx, "coder")
    canonical = f'{ctx.cli_path} agent complete {ctx.thread_id} "<summary>" --retain "<key findings>"'
    assert rendered.count(canonical) == 1
    assert "complete-agent" not in rendered
    assert "fail-agent" not in rendered
    assert "juggle complete-agent" not in rendered


@pytest.mark.parametrize("ctx", ALL_CTXS, ids=["juggle-repo-git", "foreign-repo-unbound-vcs"])
def test_zero_unbound_placeholders(ctx):
    rendered = render_agent_prompt(ctx, "coder")
    found = set(re.findall(r"<[^<>\n]+>", rendered))
    assert found <= ALLOWED_PLACEHOLDERS, f"unbound placeholders leaked: {found - ALLOWED_PLACEHOLDERS}"
    assert "{" not in rendered and "}" not in rendered


@pytest.mark.parametrize("ctx", ALL_CTXS, ids=["juggle-repo-git", "foreign-repo-unbound-vcs"])
def test_no_bare_git_outside_vcs_rendered_lines(ctx):
    rendered = render_agent_prompt(ctx, "coder")
    for line in rendered.splitlines():
        if ctx.vcs.commit_verb in line:
            continue
        assert "git " not in line.lower()


def test_render_module_has_no_hardcoded_repo_names():
    src = (SRC / "juggle_prompt_context.py").read_text()
    for name in ("trading-edge",):
        assert name not in src


@pytest.mark.parametrize("ctx", ALL_CTXS, ids=["juggle-repo-git", "foreign-repo-unbound-vcs"])
def test_previously_duplicated_rules_appear_exactly_once(ctx):
    rendered = render_agent_prompt(ctx, "coder")
    assert rendered.count("never stop at the prompt") == 1
    assert rendered.count("watchdog-owned") == 1
    assert rendered.count("Full-suite:") == 1
    assert rendered.count("Quality gate:") == 1


def test_missing_full_suite_cmd_degrades_explicitly_never_silent():
    rendered = render_agent_prompt(_foreign_ctx(), "coder")
    assert "no full-suite command configured for this repo" in rendered
    assert "your verify_cmds are the gate" in rendered


def test_version_bump_step_omitted_when_policy_absent():
    rendered = render_agent_prompt(_foreign_ctx(), "coder")
    assert "Version bump:" not in rendered


def test_version_bump_step_present_when_policy_configured():
    rendered = render_agent_prompt(_juggle_ctx(), "coder")
    assert "Version bump:" in rendered
    assert "never hand-bump" in rendered


def test_escape_hatch_only_rendered_for_juggle_repo():
    juggle_rendered = render_agent_prompt(_juggle_ctx(), "coder")
    foreign_rendered = render_agent_prompt(_foreign_ctx(), "coder")
    assert "Escape hatch" in juggle_rendered
    assert "Escape hatch" not in foreign_rendered


def test_plan_and_spec_paths_omitted_when_absent():
    rendered = render_agent_prompt(_foreign_ctx(), "coder")
    assert "Plan:" not in rendered
    assert "Spec:" not in rendered


def test_plan_and_spec_paths_rendered_when_present():
    ctx = _juggle_ctx()
    rendered = render_agent_prompt(ctx, "coder")
    assert ctx.plan_path in rendered
    assert ctx.spec_path in rendered


def test_multiple_tasks_each_get_lifecycle_steps_one_through_five():
    rendered = render_agent_prompt(_foreign_ctx(), "coder")
    for task in _foreign_ctx().tasks:
        assert f"`{task.verify_cmd}`" in rendered
        assert f"graph mark-task {task.id}" in rendered


def test_unsupported_role_raises():
    with pytest.raises(ValueError):
        render_agent_prompt(_juggle_ctx(), "planner")


# ── PC2 extensions: context narrative, verified flag, optional mark step ──────


def test_context_narrative_rendered_in_task_section_before_tasks():
    ctx = _juggle_ctx()
    ctx = PromptContext(**{**ctx.__dict__, "context": "## Project objective\nShip it."})
    rendered = render_agent_prompt(ctx, "coder")
    assert "Ship it." in rendered
    assert rendered.index("Ship it.") < rendered.index("## Lifecycle")


def test_verified_task_flagged_and_skip_note_shown():
    ctx = _foreign_ctx()
    tasks = (
        TaskSpec(id="w1", title="Add widget validation", verify_cmd="npm test",
                 body="Validate.", verified=True),
        TaskSpec(id="w2", title="Wire validation", verify_cmd="npm run test:api",
                 body="Wire it."),
    )
    ctx = PromptContext(**{**ctx.__dict__, "tasks": tasks})
    rendered = render_agent_prompt(ctx, "coder")
    assert "w1 — Add widget validation [VERIFIED — skip]" in rendered
    assert "Tasks flagged VERIFIED: skip them" in rendered
    assert "w2 — Wire validation [VERIFIED — skip]" not in rendered


def test_task_without_mark_step_omits_mark_line():
    ctx = _foreign_ctx()
    tasks = (
        TaskSpec(id="ad-hoc", title="Task", verify_cmd="", body="Do the thing.",
                 has_mark_step=False),
    )
    ctx = PromptContext(**{**ctx.__dict__, "tasks": tasks})
    rendered = render_agent_prompt(ctx, "coder")
    assert "graph mark-task" not in rendered
    assert "Run its verify_cmd" not in rendered
