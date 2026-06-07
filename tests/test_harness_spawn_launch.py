"""TDD: spawn_agent and start_agent_in_pane must honor harness_override at launch."""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

# ── shared fixtures ────────────────────────────────────────────────────────────

def _agent_cfg(harness="claude"):
    return {
        "harness": harness,
        "harnesses": {
            "claude": {
                "type": "claude",
                "readiness_markers": ["bypass permissions on"],
                "submission_markers": ["esc to interrupt"],
                "claude_launch_command": "claude --dangerously-skip-permissions",
                "env": {},
                "env_unset": [],
                "supports_hooks": True,
            },
            "reasonix": {
                "type": "template",
                "command": "reasonix run",
                "interactive": False,
                "model_flag": "--model {model}",
                "model": "deepseek-pro-direct",
                "extra_flags": "",
                "prompt_arg": "< {prompt_file}",
                "env": {},
                "env_unset": [],
                "supports_hooks": False,
            },
        },
        "role_context": {},
        "audit_mode": False,
        "settings_overlay_base": {"permissions": {"deny": []}},
        "settings_overlay_by_role": {},
    }


# ── start_agent_in_pane: uses agent_cfg param, not just config default ────────

def test_start_agent_in_pane_claude_harness_pastes_launch_command():
    """start_agent_in_pane with claude harness (interactive) writes + pastes a command."""
    from juggle_tmux import JuggleTmuxManager

    mgr = JuggleTmuxManager()
    mgr._run_tmux = MagicMock()

    cfg = _agent_cfg("claude")
    with patch("juggle_tmux._get_settings", return_value=cfg):
        mgr.start_agent_in_pane("%1", model="claude-sonnet-4-6", role="coder",
                                 agent_cfg=cfg)

    # claude is interactive → load-buffer + paste-buffer must be called
    calls = [c[0][0] for c in mgr._run_tmux.call_args_list]
    assert "load-buffer" in calls, f"Expected load-buffer, got {calls}"
    assert "paste-buffer" in calls


def test_start_agent_in_pane_reasonix_harness_is_noop():
    """start_agent_in_pane with reasonix (non-interactive) must NOT paste any command."""
    from juggle_tmux import JuggleTmuxManager

    mgr = JuggleTmuxManager()
    mgr._run_tmux = MagicMock()

    cfg = _agent_cfg("reasonix")
    with patch("juggle_tmux._get_settings", return_value=cfg):
        mgr.start_agent_in_pane("%1", model=None, role="coder", agent_cfg=cfg)

    # reasonix is non-interactive → no tmux commands should be issued
    assert mgr._run_tmux.call_count == 0, (
        f"Expected no tmux calls for non-interactive harness, got: "
        f"{mgr._run_tmux.call_args_list}"
    )


# ── spawn_agent: harness_override wires through to DB + pane launch ───────────

def test_spawn_agent_harness_override_persists_reasonix_on_agent_row():
    """spawn_agent(harness_override='reasonix') must store harness='reasonix', not 'claude'."""
    from juggle_tmux import JuggleTmuxManager

    db = MagicMock()
    db.get_all_agents.return_value = []
    created_id = "agent-uuid-1"
    db.create_agent.return_value = created_id
    db.get_agent.return_value = {"id": created_id, "pane_id": "%1", "harness": "reasonix"}

    cfg = _agent_cfg("claude")  # config default is claude
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%1"}), \
         patch("juggle_tmux._get_settings", return_value={"agent": cfg}):
        mgr = JuggleTmuxManager(session_name="juggle")
        mgr.spawn_agent(db, "coder", harness_override="reasonix")

    # create_agent must be called with harness="reasonix", not "claude"
    kw = db.create_agent.call_args[1]
    assert kw.get("harness") == "reasonix", (
        f"Expected harness='reasonix', got {kw.get('harness')!r}"
    )


def test_spawn_agent_no_override_persists_config_default_harness():
    """spawn_agent with no harness_override persists the config default harness."""
    from juggle_tmux import JuggleTmuxManager

    db = MagicMock()
    db.get_all_agents.return_value = []
    db.create_agent.return_value = "agent-uuid-2"
    db.get_agent.return_value = {"id": "agent-uuid-2", "pane_id": "%1", "harness": "claude"}

    cfg = _agent_cfg("claude")
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%1"}), \
         patch("juggle_tmux._get_settings", return_value={"agent": cfg}):
        mgr = JuggleTmuxManager(session_name="juggle")
        mgr.spawn_agent(db, "coder")  # no override

    kw = db.create_agent.call_args[1]
    assert kw.get("harness") == "claude"


def test_spawn_agent_reasonix_override_does_not_launch_interactive_pane():
    """spawn_agent with reasonix override must NOT issue load-buffer to pane (non-interactive)."""
    from juggle_tmux import JuggleTmuxManager

    db = MagicMock()
    db.get_all_agents.return_value = []
    db.create_agent.return_value = "agent-uuid-3"
    db.get_agent.return_value = {"id": "agent-uuid-3", "pane_id": "%1", "harness": "reasonix"}

    cfg = _agent_cfg("claude")  # default would launch claude without fix
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%1"}), \
         patch("juggle_tmux._get_settings", return_value={"agent": cfg}):
        mgr = JuggleTmuxManager(session_name="juggle")
        mgr._run_tmux = MagicMock()
        mgr.spawn_agent(db, "coder", harness_override="reasonix")

    calls = [c[0][0] for c in mgr._run_tmux.call_args_list]
    assert "load-buffer" not in calls, (
        f"reasonix is non-interactive: load-buffer must not be called, got {calls}"
    )


# ── cmd_send_task: adapter resolved from agent's persisted harness ─────────────

def test_cmd_send_task_resolves_adapter_from_agent_harness(tmp_path):
    """cmd_send_task dispatches via the AGENT'S persisted harness, not config default."""
    from juggle_cmd_agents import cmd_send_task

    prompt_file = tmp_path / "p.txt"
    prompt_file.write_text("do it")

    # Agent has harness="reasonix" (non-interactive)
    agent = {
        "id": "agent-1", "pane_id": "%1", "role": "coder",
        "harness": "reasonix", "repo_path": "/repo",
        "status": "busy", "assigned_thread": None, "model": None,
    }

    db = MagicMock()
    db.get_agent.return_value = agent
    db.get_thread.return_value = None

    cfg = _agent_cfg("claude")  # config default is claude; agent has reasonix
    oneshot_calls = []

    args = MagicMock()
    args.agent_id = "agent-1"
    args.prompt_file = str(prompt_file)
    args.allow_main = True   # skip worktree guard
    args.worktree_path = None
    args.worktree_branch = None
    args.main_repo_path = None
    args.no_template = True

    with patch("juggle_cmd_agents.get_db", return_value=db), \
         patch("juggle_cmd_agents.JuggleTmuxManager") as mock_cls, \
         patch("juggle_cmd_agents._get_settings", return_value={"agent": cfg}):
        mock_mgr = mock_cls.return_value
        mock_mgr.verify_pane.return_value = True
        mock_mgr.run_task_oneshot.return_value = ("hash", 1234)
        mock_mgr.send_task.return_value = "hash"

        cmd_send_task(args)

    # With reasonix (non-interactive), run_task_oneshot must be called, NOT send_task
    assert mock_mgr.run_task_oneshot.called, (
        "Expected run_task_oneshot for non-interactive reasonix agent, "
        f"but it was not called. send_task called: {mock_mgr.send_task.called}"
    )
    assert not mock_mgr.send_task.called, (
        "send_task must NOT be called for non-interactive reasonix agent"
    )
