#!/usr/bin/env python3
"""Unit tests for the self-contained Codex harness adapter."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from juggle_harness import get_adapter
from harnesses.codex import CODEX_DEFAULTS, CodexAdapter


def _cfg(**overrides):
    hcfg = {**CODEX_DEFAULTS, **overrides}
    return {"harness": "codex", "harnesses": {"codex": hcfg}}


def test_codex_type_resolves():
    adapter = get_adapter("coder", agent_cfg=_cfg())
    assert isinstance(adapter, CodexAdapter)
    assert adapter.id == "codex"
    assert adapter.supports_hooks is False  # version-skewed hooks → inline anchor


def test_codex_sandbox_per_role():
    cfg = _cfg()
    coder = get_adapter("coder", agent_cfg=cfg).build_launch_command(role="coder", model="gpt-5")
    researcher = get_adapter("researcher", agent_cfg=cfg).build_launch_command(role="researcher")
    planner = get_adapter("planner", agent_cfg=cfg).build_launch_command(role="planner")
    assert "-s workspace-write" in coder
    assert "-s read-only" in researcher
    assert "-s read-only" in planner
    # approval policy + model
    assert "-a never" in coder
    assert "-m gpt-5" in coder


def test_codex_unknown_role_uses_default_sandbox():
    cmd = get_adapter("weird", agent_cfg=_cfg()).build_launch_command(role="weird")
    assert "-s read-only" in cmd  # sandbox_default


def test_codex_audit_relaxes_sandbox():
    cmd = get_adapter("researcher", agent_cfg=_cfg()).build_launch_command(
        role="researcher", audit=True
    )
    # researcher is normally read-only; audit relaxes to workspace-write so
    # tool demand becomes observable (mirrors Claude's deny-relaxation).
    assert "-s workspace-write" in cmd
    assert "JUGGLE_AGENT_AUDIT=1" in cmd


def test_codex_identity_env_present():
    cmd = get_adapter("coder", agent_cfg=_cfg()).build_launch_command(role="coder")
    assert "JUGGLE_IS_AGENT=1" in cmd
    assert "JUGGLE_AGENT_ROLE=coder" in cmd


def test_codex_extra_restriction_flag_appended():
    cmd = get_adapter("coder", agent_cfg=_cfg(restrictions_flag='-c foo="bar"')).build_launch_command(
        role="coder"
    )
    assert '-c foo="bar"' in cmd


def test_codex_inlines_role_anchor():
    adapter = get_adapter("coder", agent_cfg=_cfg())
    with patch("juggle_context.render_agent_role_anchor_for", return_value="CODEX-ANCHOR"):
        out = adapter.decorate_task("coder", "BUILD IT")
    assert out == "CODEX-ANCHOR\n\nBUILD IT"


def test_codex_hook_capable_override_skips_inline():
    """A deployment on a hook-capable Codex sets supports_hooks=true → no inline."""
    adapter = get_adapter("coder", agent_cfg=_cfg(supports_hooks=True))
    with patch("juggle_context.render_agent_role_anchor_for", return_value="CODEX-ANCHOR"):
        out = adapter.decorate_task("coder", "BUILD IT")
    assert out == "BUILD IT"


def test_codex_overridable_command_and_markers():
    cfg = _cfg(command="codex-next", readiness_markers=["READY"], submission_markers=["GO"])
    adapter = get_adapter("coder", agent_cfg=cfg)
    assert adapter.readiness_markers() == ("READY",)
    assert adapter.submission_markers() == ("GO",)
    assert "codex-next" in adapter.build_launch_command(role="coder")


# --- one-shot (non-interactive) dispatch ----------------------------------
def test_codex_is_non_interactive():
    adapter = get_adapter("coder", agent_cfg=_cfg())
    assert adapter.is_interactive is False


def test_codex_task_command_uses_exec_and_prompt_file():
    adapter = get_adapter("coder", agent_cfg=_cfg())
    cmd = adapter.build_task_command("/tmp/task42.txt", role="coder", model="gpt-5")
    # one-shot: `codex exec ... "$(cat <file>)"`, single line
    assert "codex exec" in cmd
    assert "-s workspace-write" in cmd
    assert '"$(cat /tmp/task42.txt)"' in cmd
    assert "\n" not in cmd
    assert "JUGGLE_IS_AGENT=1" in cmd and "JUGGLE_AGENT_ROLE=coder" in cmd


def test_codex_task_command_per_role_sandbox():
    adapter = get_adapter("researcher", agent_cfg=_cfg())
    cmd = adapter.build_task_command("/tmp/t.txt", role="researcher")
    assert "-s read-only" in cmd


def test_run_task_oneshot_pastes_command_and_no_marker_wait():
    """JuggleTmuxManager.run_task_oneshot pastes the one-shot command and never
    polls for REPL readiness/submission markers."""
    from unittest.mock import MagicMock
    from juggle_tmux import JuggleTmuxManager

    mgr = JuggleTmuxManager(session_name="juggle-test")
    pasted = []

    def fake_tmux(*args):
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        if args[0] == "load-buffer":
            try:
                pasted.append(Path(args[-1]).read_text())
            except Exception:
                pass
        return m

    cfg = {"harness": "codex", "harnesses": {"codex": CODEX_DEFAULTS}}
    with (
        patch.object(mgr, "_run_tmux", side_effect=fake_tmux),
        patch("juggle_tmux._get_settings", return_value={"agent": cfg}),
        patch.object(mgr, "wait_for_ready_to_paste") as ready,
        patch.object(mgr, "wait_for_submission") as submit,
    ):
        mgr.run_task_oneshot("%5", "DO THE TASK", role="coder", model="gpt-5")

    assert pasted, "one-shot command was never pasted"
    assert "codex exec" in pasted[0]
    assert '"$(cat ' in pasted[0]  # prompt supplied via file
    ready.assert_not_called()
    submit.assert_not_called()
