"""Spawn safety fallback: a bad/unavailable resolved model id must NEVER break
agent dispatch (T-coder-model-resolution, MANDATORY SAFETY).

If the cascade-resolved model fails to boot (interactive pane never reaches the
ready marker — e.g. the harness silently rejects an invalid model id), spawn
must retry ONCE on the harness default model (no --model flag) and log a warning,
rather than raising and leaving the tick unable to dispatch.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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


class _InteractiveStub:
    id = "claude"
    is_interactive = True

    def build_launch_command(self, role=None, model=None, audit=False, effort=None):
        return "claude --dangerously-skip-permissions"


def _make_db():
    db = MagicMock()
    db.get_all_agents.return_value = []
    db.create_agent.return_value = "a1"
    db.get_agent.return_value = {"id": "a1", "pane_id": "%2", "harness": "claude"}
    return db


def _settings():
    # coder tier resolves to a model that (in this test) fails to boot.
    return {"agent": _cfg("claude"),
            "agents": {"by_role": {"coder": {"model": "haiku", "effort": "low"}}}}


def test_spawn_falls_back_to_default_model_when_resolved_model_fails(caplog):
    import juggle_harness
    from juggle_tmux import JuggleTmuxManager

    launched = []

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JUGGLE_TMUX_MOCK_PANE", None)
        mgr = JuggleTmuxManager(session_name="juggle")
        mgr.ensure_session = MagicMock()
        mgr.spawn_pane = MagicMock(side_effect=["%1", "%2"])
        mgr.kill_pane = MagicMock()

        def _start(pane_id, model=None, role=None, agent_cfg=None, effort=None):
            launched.append(model)
        mgr.start_agent_in_pane = MagicMock(side_effect=_start)
        # First boot (resolved model) fails to ready; fallback boot succeeds.
        mgr.wait_for_ready_to_paste = MagicMock(side_effect=[False, True])

        db = _make_db()
        with patch.object(juggle_harness, "get_adapter",
                          lambda role, agent_cfg=None: _InteractiveStub()), \
             patch("juggle_tmux._get_settings", return_value=_settings()), \
             patch("juggle_tmux._spawn_repo_path", return_value="/repo"), \
             patch("juggle_tmux._pretrust_spawn_dir"), \
             caplog.at_level("WARNING"):
            agent = mgr.spawn_agent(db, "coder")

    # Two boot attempts: first with the resolved model, then the safe default.
    assert launched == ["haiku", None]
    mgr.kill_pane.assert_called_once_with("%1")
    # Agent registered on the second (successful) pane — dispatch NOT broken.
    db.create_agent.assert_called_once()
    assert db.create_agent.call_args.kwargs["pane_id"] == "%2"
    assert agent is not None
    assert any("fall" in r.message.lower() or "fallback" in r.message.lower()
               for r in caplog.records), "expected a warning about model fallback"
    # Defect E (2026-07-01): after fallback the agent must record the ACTUAL
    # launch model (harness default → None), NOT the requested model — the agent
    # list showed 'sonnet' while the pane ran the default. The last update_agent
    # for this agent must store model=None.
    model_updates = [c for c in db.update_agent.call_args_list
                     if "model" in c.kwargs]
    assert model_updates, "spawn must persist the launch model"
    assert model_updates[-1].kwargs["model"] is None


def test_spawn_raises_when_even_default_model_fails():
    """Fallback is best-effort: if the default model ALSO never readies (e.g.
    stuck at trust), spawn still raises so the tick can retry — no zombie pane."""
    import juggle_harness
    from juggle_tmux import JuggleTmuxManager

    os.environ.pop("JUGGLE_TMUX_MOCK_PANE", None)
    mgr = JuggleTmuxManager(session_name="juggle")
    mgr.ensure_session = MagicMock()
    mgr.spawn_pane = MagicMock(side_effect=["%1", "%2"])
    mgr.kill_pane = MagicMock()
    mgr.start_agent_in_pane = MagicMock()
    mgr.wait_for_ready_to_paste = MagicMock(return_value=False)

    db = _make_db()
    with patch.object(juggle_harness, "get_adapter",
                      lambda role, agent_cfg=None: _InteractiveStub()), \
         patch("juggle_tmux._get_settings", return_value=_settings()), \
         patch("juggle_tmux._spawn_repo_path", return_value="/repo"), \
         patch("juggle_tmux._pretrust_spawn_dir"):
        with pytest.raises(RuntimeError):
            mgr.spawn_agent(db, "coder")
    # Both panes cleaned up, no agent registered.
    assert mgr.kill_pane.call_count == 2
    db.create_agent.assert_not_called()
