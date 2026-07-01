"""Resolver → spawn/launch wiring (2026-06-30 agent model/effort config)."""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _cfg(harness="claude"):
    return {
        "harness": harness,
        "harnesses": {
            "claude": {
                "type": "claude",
                "readiness_markers": ["bypass permissions on"],
                "submission_markers": ["esc to interrupt"],
                "claude_launch_command": "claude --dangerously-skip-permissions",
                "env": {}, "env_unset": [], "supports_hooks": True,
            },
        },
        "role_context": {}, "audit_mode": False,
        "settings_overlay_base": {"permissions": {"deny": []}},
        "settings_overlay_by_role": {},
    }


def test_spawn_agent_stores_cascade_resolved_model():
    """2026-06-30 agent model/effort config: spawn resolves by_role model + stores it."""
    from juggle_tmux import JuggleTmuxManager

    db = MagicMock()
    db.get_all_agents.return_value = []
    db.create_agent.return_value = "a1"
    db.get_agent.return_value = {"id": "a1", "pane_id": "%1", "harness": "claude"}
    settings = {"agent": _cfg("claude"),
                "agents": {"by_role": {"coder": {"model": "haiku", "effort": "low"}}}}
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%1"}), \
         patch("juggle_tmux._get_settings", return_value=settings):
        JuggleTmuxManager(session_name="juggle").spawn_agent(db, "coder")
    stored = [c.kwargs.get("model") for c in db.update_agent.call_args_list
              if "model" in c.kwargs]
    assert "haiku" in stored


def test_start_agent_in_pane_forwards_effort_to_launch(monkeypatch):
    """2026-06-30 agent model/effort config: start_agent_in_pane threads effort into the launch."""
    import juggle_harness
    from juggle_tmux import JuggleTmuxManager

    captured = {}

    class _Stub:
        id = "claude"
        is_interactive = True

        def build_launch_command(self, role=None, model=None, audit=False, effort=None):
            captured.update(model=model, effort=effort)
            return "claude --model x --effort high"

    monkeypatch.setattr(juggle_harness, "get_adapter", lambda role, agent_cfg=None: _Stub())
    mgr = JuggleTmuxManager()
    mgr._run_tmux = MagicMock()
    cfg = _cfg("claude")
    with patch("juggle_tmux._get_settings", return_value={"agent": cfg}):
        mgr.start_agent_in_pane("%1", model="sonnet", role="coder", agent_cfg=cfg, effort="high")
    assert captured["effort"] == "high"
