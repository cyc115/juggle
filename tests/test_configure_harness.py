#!/usr/bin/env python3
"""Tests for `juggle configure-harness` (cmd_configure_harness)."""

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from juggle_cmd_context import cmd_configure_harness


def _args(harness, role=None, model=None, extra_flags=None, command=None):
    return types.SimpleNamespace(
        harness=harness, role=role, model=model, extra_flags=extra_flags, command=command
    )


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(p))
    return p


def _load(p):
    return json.loads(p.read_text())


def test_sets_global_harness_seeded_from_defaults(cfg_path, capsys):
    cmd_configure_harness(_args("codex"))
    agent = _load(cfg_path)["agent"]
    assert agent["harness"] == "codex"
    # codex def seeded from DEFAULTS (one-shot exec form present)
    assert agent["harnesses"]["codex"]["command"] == "codex exec"
    assert "global" in capsys.readouterr().out


def test_pins_model_and_extra_flags(cfg_path):
    cmd_configure_harness(_args("codex", model="gpt-5-codex", extra_flags="-c r=high"))
    hdef = _load(cfg_path)["agent"]["harnesses"]["codex"]
    assert hdef["model"] == "gpt-5-codex"
    assert hdef["extra_flags"] == "-c r=high"


def test_per_role_override_does_not_touch_global(cfg_path):
    cmd_configure_harness(_args("codex", role="researcher"))
    agent = _load(cfg_path)["agent"]
    assert agent["harness_by_role"]["researcher"] == "codex"
    assert "harness" not in agent or agent["harness"] != "codex"


def test_unknown_harness_errors(cfg_path):
    with pytest.raises(SystemExit) as e:
        cmd_configure_harness(_args("bogus"))
    assert e.value.code == 1
    assert not cfg_path.exists()  # nothing written on error


def test_unknown_harness_with_command_creates_template(cfg_path):
    cmd_configure_harness(_args("myharness", command="myharness run"))
    hdef = _load(cfg_path)["agent"]["harnesses"]["myharness"]
    assert hdef["type"] == "template"
    assert hdef["command"] == "myharness run"


def test_preserves_existing_config(cfg_path):
    cfg_path.write_text(json.dumps({"hindsight": {"enabled": True}, "agent": {"audit_mode": True}}))
    cmd_configure_harness(_args("codex", model="gpt-5"))
    cfg = _load(cfg_path)
    assert cfg["hindsight"]["enabled"] is True  # untouched
    assert cfg["agent"]["audit_mode"] is True  # untouched
    assert cfg["agent"]["harness"] == "codex"


def test_written_config_resolves_codex_adapter(cfg_path):
    """End-to-end: configure → get_settings → adapter resolution."""
    cmd_configure_harness(_args("codex", model="gpt-5-codex"))
    from juggle_settings import get_settings
    import juggle_harness as jh

    agent = get_settings()["agent"]
    adapter = jh.get_adapter("coder", agent_cfg=agent)
    cmd = adapter.build_task_command("/tmp/p.txt", role="coder", model="sonnet")
    assert type(adapter).__name__ == "CodexAdapter"
    assert "-m gpt-5-codex" in cmd  # pinned model overrides passed model
