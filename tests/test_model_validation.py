"""Regression pin — incident KB (2026-07-19): an invalid --model string (e.g.
"claude/opus" instead of "opus") reached the Claude Code harness at boot, the
harness silently rejected it, and the agent was NEVER processed — yet dispatch
had already written status='busy', so it sat busy at ~0 tokens forever. Worse,
`agent release` returned it to the idle pool STILL carrying the bad model, so
the next `agent get` reuse re-jammed.

Fix: validate + normalize the model string at the single choke point where an
agent's launch model is resolved (JuggleTmuxManager.spawn_agent, before any
DB/pool/boot work) — reject unrecognized models fail-loud BEFORE status='busy'
is ever written. Also guard the idle-pool checkout path (_reuse_idle_agent) so
a pool agent poisoned with an invalid model (e.g. legacy data pre-dating this
fix) is decommissioned rather than handed back.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "model_validation.db"))
    d.init_db()
    d.set_active(True)
    return d


@pytest.fixture
def thread_id(db):
    return db.create_thread("model-validation-test", session_id="")


def _fake_mgr():
    mgr = MagicMock()
    mgr.wait_for_ready_to_paste.return_value = True
    mgr._run_tmux.return_value = MagicMock(returncode=0, stdout="")
    mgr.verify_pane.return_value = True
    return mgr


def _spawning_mgr():
    """Mirrors test_model_preference._spawning_mgr: a real idle agent is
    created in the DB on spawn, so post-spawn update_agent has a real row."""
    mgr = _fake_mgr()

    def fake_spawn(db_, role, *, model=None, harness_override=None, effort=None):
        aid = db_.create_agent(
            role=role, pane_id="%spawned",
            harness=harness_override or "claude", repo_path="",
        )
        db_.update_agent(aid, model=model)
        return db_.get_agent(aid)

    mgr.spawn_agent.side_effect = fake_spawn
    return mgr


def _make_idle_agent(db, *, model, role="coder", pane="%pool", harness="claude"):
    aid = db.create_agent(role=role, pane_id=pane, harness=harness, repo_path="")
    db.update_agent(aid, model=model)
    return aid


# ── JuggleTmuxManager.spawn_agent — the single choke point ─────────────────


def _settings_stub():
    return {
        "agent": {
            "harness": "claude",
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
        },
        "agents": {"by_role": {}},
    }


class _InteractiveStub:
    id = "claude"
    is_interactive = True

    def build_launch_command(self, role=None, model=None, audit=False, effort=None):
        return "claude --dangerously-skip-permissions"


def test_spawn_agent_rejects_invalid_model_before_any_dispatch_side_effect(monkeypatch):
    """An unrecognized model must be rejected loudly at spawn time — BEFORE the
    pool is touched, a pane is booted, or an agent row is created. This is what
    prevents the poisoned status='busy' zombie."""
    import juggle_harness
    from juggle_tmux import JuggleTmuxManager

    mgr = JuggleTmuxManager(session_name="juggle")
    mgr.ensure_session = MagicMock()
    mgr.spawn_pane = MagicMock()
    mgr.kill_pane = MagicMock()
    mgr.start_agent_in_pane = MagicMock()
    mgr.wait_for_ready_to_paste = MagicMock(return_value=True)

    db = MagicMock()
    db.get_all_agents.return_value = []

    with monkeypatch.context() as m:
        m.setattr(juggle_harness, "get_adapter",
                  lambda role, agent_cfg=None: _InteractiveStub())
        m.setattr("juggle_tmux._get_settings", _settings_stub)
        m.setattr("juggle_tmux._spawn_repo_path", lambda: "/repo")
        m.setattr("juggle_tmux._pretrust_spawn_dir", lambda *_: None)

        with pytest.raises(ValueError, match="opus"):
            mgr.spawn_agent(db, "coder", model="claude/opus/whatever")

    # No pane ever booted, no agent row ever created — the bug's root cause.
    mgr.spawn_pane.assert_not_called()
    db.create_agent.assert_not_called()


def test_spawn_agent_normalizes_claude_slash_prefix(monkeypatch):
    """'claude/opus' is an unambiguous typo of 'opus' (redundant provider
    prefix) — normalize and boot successfully rather than rejecting it."""
    import juggle_harness
    from juggle_tmux import JuggleTmuxManager

    mgr = JuggleTmuxManager(session_name="juggle")
    mgr.ensure_session = MagicMock()
    mgr.spawn_pane = MagicMock(return_value="%1")
    mgr.kill_pane = MagicMock()
    mgr.start_agent_in_pane = MagicMock()
    mgr.wait_for_ready_to_paste = MagicMock(return_value=True)

    db = MagicMock()
    db.get_all_agents.return_value = []
    db.create_agent.return_value = "a1"
    db.get_agent.return_value = {"id": "a1", "pane_id": "%1", "harness": "claude"}

    with monkeypatch.context() as m:
        m.setattr(juggle_harness, "get_adapter",
                  lambda role, agent_cfg=None: _InteractiveStub())
        m.setattr("juggle_tmux._get_settings", _settings_stub)
        m.setattr("juggle_tmux._spawn_repo_path", lambda: "/repo")
        m.setattr("juggle_tmux._pretrust_spawn_dir", lambda *_: None)

        agent = mgr.spawn_agent(db, "coder", model="claude/opus")

    assert agent is not None
    model_updates = [c for c in db.update_agent.call_args_list if "model" in c.kwargs]
    assert model_updates[-1].kwargs["model"] == "opus"


# ── acquire_agent — invalid model must never leave a busy/poisoned agent ────


def test_acquire_agent_invalid_model_raises_and_writes_no_busy_status(db, thread_id, monkeypatch):
    from juggle_dispatch_core import acquire_agent

    monkeypatch.setattr("juggle_db.MAX_BACKGROUND_AGENTS", 5)
    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")

    mgr = _fake_mgr()
    mgr.spawn_agent.side_effect = ValueError("invalid model 'claude/sonnet'")

    with pytest.raises(RuntimeError, match="invalid model"):
        acquire_agent(db, thread_id, role="coder", model="claude/sonnet",
                      harness="claude", _mgr=mgr)

    assert db.get_all_agents() == [], "a rejected model must never create an agent row"
    thread = db.get_thread(thread_id)
    assert thread["state"] != "background", "a rejected dispatch must not mark the thread busy"


def test_reuse_idle_agent_decommissions_poisoned_model_instead_of_reusing(
    db, thread_id, monkeypatch
):
    """A pool agent poisoned with an invalid stored model (legacy data, or a
    pre-fix write) must be decommissioned on checkout — never handed back to
    `agent get`, which would re-jam the next dispatch."""
    from juggle_dispatch_core import acquire_agent

    monkeypatch.setattr("juggle_db.MAX_BACKGROUND_AGENTS", 5)
    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")
    poisoned = _make_idle_agent(db, model="claude/opus", pane="%poisoned")

    mgr = _spawning_mgr()
    got = acquire_agent(db, thread_id, role="coder", model=None,
                        harness="claude", _mgr=mgr)

    assert got["id"] != poisoned, "poisoned-model agent must never be reused"
    mgr.decommission_agent.assert_called_once_with(db, poisoned)
    assert db.get_agent(poisoned) is None, "poisoned agent must be removed from the pool"
