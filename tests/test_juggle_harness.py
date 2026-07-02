#!/usr/bin/env python3
"""Tests for juggle_harness — pluggable sub-agent harness adapters."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import juggle_harness
from juggle_harness import TemplateHarnessAdapter, get_adapter
from harnesses.claude import ClaudeCodeAdapter


# --- selection ------------------------------------------------------------
def test_get_adapter_default_is_claude():
    adapter = get_adapter(agent_cfg={})
    assert isinstance(adapter, ClaudeCodeAdapter)
    assert adapter.id == "claude"
    assert adapter.supports_hooks is True


def test_get_adapter_unknown_harness_falls_back_to_claude():
    """Selecting an id with no definition synthesises the built-in claude harness."""
    adapter = get_adapter(agent_cfg={"harness": "does-not-exist"})
    assert isinstance(adapter, ClaudeCodeAdapter)
    assert adapter.id == "claude"


def test_get_adapter_per_role_override():
    cfg = {
        "harness": "claude",
        "harness_by_role": {"researcher": "codex"},
        "harnesses": {
            "codex": {"type": "template", "command": "codex"},
        },
    }
    # The override role gets codex; everyone else gets the default (claude).
    assert isinstance(get_adapter("researcher", agent_cfg=cfg), TemplateHarnessAdapter)
    assert get_adapter("researcher", agent_cfg=cfg).id == "codex"
    assert isinstance(get_adapter("coder", agent_cfg=cfg), ClaudeCodeAdapter)


# --- claude command construction (zero behaviour change) ------------------
def test_claude_build_launch_command_matches_legacy():
    """A legacy config (no `harnesses` block) yields the exact historical command."""
    cfg = {"claude_launch_command": "claude --dangerously-skip-permissions"}
    adapter = get_adapter("coder", agent_cfg=cfg)
    with patch("juggle_agent_settings.write_agent_overlay") as mock_overlay:
        mock_overlay.return_value = Path("/tmp/coder.json")
        cmd = adapter.build_launch_command(role="coder", model="sonnet", audit=False)
    assert cmd == (
        "env -u CLAUDE_PLUGIN_DATA JUGGLE_IS_AGENT=1 JUGGLE_AGENT_ROLE=coder "
        "claude --dangerously-skip-permissions --model sonnet --settings /tmp/coder.json"
    )
    mock_overlay.assert_called_once_with("coder")


def test_claude_audit_env_appended():
    cfg = {"claude_launch_command": "claude --dangerously-skip-permissions"}
    adapter = get_adapter("coder", agent_cfg=cfg)
    with patch("juggle_agent_settings.write_agent_overlay") as mock_overlay:
        mock_overlay.return_value = Path("/tmp/coder.json")
        cmd = adapter.build_launch_command(role="coder", model="sonnet", audit=True)
    assert "JUGGLE_AGENT_AUDIT=1" in cmd
    assert "JUGGLE_IS_AGENT=1" in cmd


def test_claude_no_model_omits_model_flag():
    cfg = {"claude_launch_command": "claude --dangerously-skip-permissions"}
    adapter = get_adapter(None, agent_cfg=cfg)
    with patch("juggle_agent_settings.write_agent_overlay") as mock_overlay:
        mock_overlay.return_value = Path("/tmp/default.json")
        cmd = adapter.build_launch_command(role=None, model=None, audit=False)
    assert "--model" not in cmd
    assert "JUGGLE_AGENT_ROLE" not in cmd  # no role → no role env
    assert cmd.endswith("--settings /tmp/default.json")


def test_claude_effort_flag_injected():
    """2026-06-30 agent model/effort config: effort injects as --effort <level>."""
    cfg = {"claude_launch_command": "claude --dangerously-skip-permissions"}
    adapter = get_adapter("coder", agent_cfg=cfg)
    with patch("juggle_agent_settings.write_agent_overlay") as mock_overlay:
        mock_overlay.return_value = Path("/tmp/coder.json")
        cmd = adapter.build_launch_command(role="coder", model="sonnet", effort="high")
    assert "--effort high" in cmd


def test_claude_no_effort_omits_effort_flag():
    """2026-06-30 agent model/effort config: effort=None omits the flag (legacy-identical)."""
    cfg = {"claude_launch_command": "claude --dangerously-skip-permissions"}
    adapter = get_adapter("coder", agent_cfg=cfg)
    with patch("juggle_agent_settings.write_agent_overlay") as mock_overlay:
        mock_overlay.return_value = Path("/tmp/coder.json")
        cmd = adapter.build_launch_command(role="coder", model="sonnet", effort=None)
    assert "--effort" not in cmd


# --- template (config-only) harness --------------------------------------
def _codex_cfg():
    return {
        "harness": "codex",
        "harnesses": {
            "codex": {
                "type": "template",
                "command": "codex",
                "model_flag": "-m {model}",
                "restrictions_flag": "--sandbox ro",
                "env": {"JUGGLE_IS_AGENT": "1"},
                "env_unset": [],
                "readiness_markers": ["» "],
                "submission_markers": ["Esc to interrupt"],
                "supports_hooks": False,
            }
        },
    }


def test_template_build_launch_command():
    adapter = get_adapter("coder", agent_cfg=_codex_cfg())
    cmd = adapter.build_launch_command(role="coder", model="gpt-5", audit=False)
    assert cmd == "env JUGGLE_IS_AGENT=1 JUGGLE_AGENT_ROLE=coder codex -m gpt-5 --sandbox ro"


def test_template_markers_from_config():
    adapter = get_adapter("coder", agent_cfg=_codex_cfg())
    assert adapter.readiness_markers() == ("» ",)
    assert adapter.submission_markers() == ("Esc to interrupt",)
    assert adapter.supports_hooks is False


def test_template_pinned_model_and_extra_flags():
    """Base config knobs: `model` overrides the passed model; `extra_flags`
    are appended verbatim. Available to every harness, not just Codex."""
    cfg = _codex_cfg()
    cfg["harnesses"]["codex"]["model"] = "pinned-1"
    cfg["harnesses"]["codex"]["extra_flags"] = "--foo bar"
    adapter = get_adapter("coder", agent_cfg=cfg)
    cmd = adapter.build_launch_command(role="coder", model="ignored", audit=False)
    assert "-m pinned-1" in cmd and "ignored" not in cmd
    assert cmd.endswith("--foo bar")


# --- task decoration (anchor inlining for non-hook harnesses) -------------
def test_decorate_task_claude_is_noop():
    adapter = get_adapter("coder", agent_cfg={})
    assert adapter.decorate_task("coder", "DO THING") == "DO THING"


def test_decorate_task_inlines_anchor_for_non_hook_harness():
    adapter = get_adapter("coder", agent_cfg=_codex_cfg())
    with patch("juggle_context.render_agent_role_anchor_for", return_value="ANCHOR"):
        out = adapter.decorate_task("coder", "DO THING")
    assert out == "ANCHOR\n\nDO THING"


def test_decorate_task_non_hook_without_anchor_returns_prompt():
    adapter = get_adapter("coder", agent_cfg=_codex_cfg())
    with patch("juggle_context.render_agent_role_anchor_for", return_value=""):
        out = adapter.decorate_task("coder", "DO THING")
    assert out == "DO THING"


# --- shipped defaults are wired correctly --------------------------------
def test_real_settings_default_harness_is_claude():
    """With the shipped DEFAULTS, the global default adapter is Claude Code."""
    from juggle_harness_defaults import HARNESS_DEFAULTS

    adapter = get_adapter()
    assert adapter.id == "claude"
    assert adapter.readiness_markers() == tuple(HARNESS_DEFAULTS["claude"]["readiness_markers"])
    assert adapter.submission_markers() == tuple(HARNESS_DEFAULTS["claude"]["submission_markers"])
