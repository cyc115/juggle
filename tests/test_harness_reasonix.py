#!/usr/bin/env python3
"""Tests for Reasonix (deepseek-reasonix) harness support.

Reasonix is a config-only `template` harness (no dedicated adapter module): a
one-shot `reasonix run` reading the prompt from stdin, with tool restriction
delegated to its own reasonix.toml (external_restriction).
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from juggle_harness import get_adapter, TemplateHarnessAdapter
from juggle_settings import DEFAULTS


def _cfg(**overrides):
    hcfg = {**DEFAULTS["agent"]["harnesses"]["reasonix"], **overrides}
    return {"harness": "reasonix", "harnesses": {"reasonix": hcfg}}


def test_reasonix_is_shipped_and_config_only():
    adapter = get_adapter("coder", agent_cfg=_cfg())
    assert isinstance(adapter, TemplateHarnessAdapter)  # no custom module needed
    assert adapter.id == "reasonix"


def test_reasonix_is_one_shot_stdin_command():
    adapter = get_adapter("coder", agent_cfg=_cfg())
    assert adapter.is_interactive is False
    cmd = adapter.build_task_command("/tmp/t.txt", role="coder")
    assert "reasonix run" in cmd
    assert cmd.endswith("< /tmp/t.txt")  # prompt fed via stdin
    assert "\n" not in cmd
    assert "JUGGLE_IS_AGENT=1" in cmd and "JUGGLE_AGENT_ROLE=coder" in cmd


def test_reasonix_pins_model_overriding_agent_model():
    cmd = get_adapter("coder", agent_cfg=_cfg()).build_task_command(
        "/tmp/t.txt", role="coder", model="sonnet"
    )
    assert "--model deepseek-v4-pro" in cmd  # pinned OpenRouter provider name wins
    assert "sonnet" not in cmd
    # ...and is overridable
    cmd2 = get_adapter("coder", agent_cfg=_cfg(model="mimo-pro")).build_task_command(
        "/tmp/t.txt", role="coder"
    )
    assert "--model mimo-pro" in cmd2


def test_reasonix_restriction_is_external():
    adapter = get_adapter("coder", agent_cfg=_cfg())
    assert adapter.external_restriction is True
    assert adapter._restrictions_part("coder", False) == ""  # no juggle-applied flags


def test_reasonix_inlines_role_anchor():
    adapter = get_adapter("coder", agent_cfg=_cfg())
    assert adapter.supports_hooks is False
    with patch("juggle_context.render_agent_role_anchor_for", return_value="R-ANCHOR"):
        out = adapter.decorate_task("coder", "DO IT")
    assert out == "R-ANCHOR\n\nDO IT"


def test_reasonix_env_overridable():
    cmd = get_adapter("coder", agent_cfg=_cfg(env={"DEEPSEEK_API_KEY": "sk-x"})).build_task_command(
        "/tmp/t.txt", role="coder"
    )
    assert "DEEPSEEK_API_KEY=sk-x" in cmd
