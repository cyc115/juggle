"""Regression tests for get-agent busy-guard (2026-06-15).

Symptom: get-agent ZT reassigned a BUSY agent (status=busy, assigned_thread=ZY)
to ZT, orphaning ZY's in-flight work.

Fix goals:
1. get_ranked_idle_agents must exclude agents with status!='idle' OR assigned_thread IS NOT NULL.
2. cas_assign_agent must be atomic: UPDATE ... WHERE status='idle' AND assigned_thread IS NULL.
3. cmd_get_agent must use CAS; if CAS rowcount=0 (agent taken), spawn fresh.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


def test_ranked_idle_excludes_busy_agent(db):
    """A busy agent is never included in the ranked-idle list.

    2026-06-15: get-agent ZT reused an agent that was status=busy,
    assigned_thread=ZY — orphaning ZY's in-flight work.
    """
    busy_id = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(busy_id, status="busy", assigned_thread="thread-ZY")

    result = db.get_ranked_idle_agents("thread-ZT", role="coder")

    assert result == [], "busy agent must never appear in ranked-idle list"
    busy = db.get_agent(busy_id)
    assert busy["assigned_thread"] == "thread-ZY"
    assert busy["status"] == "busy"


def test_ranked_idle_excludes_idle_with_nonnull_assigned_thread(db):
    """An idle agent with non-null assigned_thread must be excluded.

    2026-06-15: A half-released agent (status=idle but assigned_thread!=NULL)
    could be double-assigned to a second thread.
    """
    stale_id = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(stale_id, status="idle", assigned_thread="thread-ZY")

    result = db.get_ranked_idle_agents("thread-ZT", role="coder")

    assert result == [], "idle agent with non-null assigned_thread must not be selectable"


def test_ranked_idle_returns_clean_idle_agent(db):
    """A clean idle agent (status=idle, assigned_thread=NULL) IS returned."""
    good_id = db.create_agent(role="coder", pane_id="%1")

    result = db.get_ranked_idle_agents("thread-ZT", role="coder")

    assert len(result) == 1
    assert result[0]["id"] == good_id


def test_cas_assign_succeeds_on_clean_idle_agent(db):
    """cas_assign_agent returns True and marks the agent busy+assigned."""
    agent_id = db.create_agent(role="coder", pane_id="%1")

    ok = db.cas_assign_agent(agent_id, thread_id="thread-ZT")

    assert ok is True
    updated = db.get_agent(agent_id)
    assert updated["status"] == "busy"
    assert updated["assigned_thread"] == "thread-ZT"


def test_cas_assign_fails_if_agent_already_busy(db):
    """cas_assign_agent returns False when agent was already taken.

    2026-06-15: TOCTOU race — two concurrent get-agent calls could both
    read the same idle agent; the second CAS must fail.
    """
    agent_id = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(agent_id, status="busy", assigned_thread="thread-ZY")

    ok = db.cas_assign_agent(agent_id, thread_id="thread-ZT")

    assert ok is False
    agent = db.get_agent(agent_id)
    assert agent["assigned_thread"] == "thread-ZY"
    assert agent["status"] == "busy"


def test_cas_assign_fails_if_agent_has_nonnull_assigned_thread(db):
    """cas_assign_agent returns False when assigned_thread IS NOT NULL, even if idle."""
    agent_id = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(agent_id, status="idle", assigned_thread="thread-ZY")

    ok = db.cas_assign_agent(agent_id, thread_id="thread-ZT")

    assert ok is False
    agent = db.get_agent(agent_id)
    assert agent["assigned_thread"] == "thread-ZY"


def _settings(harness="claude"):
    return {"agent": {"harness": harness, "harnesses": {"claude": {"type": "claude"}}}}


def _get_args(**kw):
    args = MagicMock()
    args.thread_id = kw.get("thread_id", "ZT")
    args.role = kw.get("role", "coder")
    args.repo = kw.get("repo", "/repo")
    args.harness = kw.get("harness", None)
    args.fresh = kw.get("fresh", False)
    args.model = kw.get("model", None)
    return args


def test_cmd_get_agent_spawns_fresh_when_only_busy_agent_exists():
    """cmd_get_agent must spawn fresh when only busy agents exist.

    2026-06-15: get-agent ZT reassigned a busy agent from ZY, orphaning ZY.
    """
    from juggle_cmd_agents import cmd_get_agent

    spawned = {
        "id": "agent-new", "pane_id": "%2", "role": "coder",
        "harness": "claude", "repo_path": "/repo",
        "status": "idle", "assigned_thread": None, "model": None,
    }
    db = MagicMock()
    db.get_all_agents.return_value = [{
        "id": "agent-busy", "pane_id": "%1", "role": "coder",
        "harness": "claude", "repo_path": "/repo",
        "status": "busy", "assigned_thread": "thread-ZY", "model": None,
    }]
    db.get_ranked_idle_agents.return_value = []
    db.get_thread.return_value = {
        "id": "t-ZT", "user_label": "ZT", "worktree_path": "",
        "worktree_branch": "", "main_repo_path": "", "open_questions": "[]",
    }
    mock_cls = MagicMock()
    mock_mgr = mock_cls.return_value
    mock_mgr.spawn_agent.return_value = spawned

    with patch("juggle_cmd_agents_common.get_db", return_value=db), \
         patch("juggle_tmux.JuggleTmuxManager", mock_cls), \
         patch("juggle_cmd_agents_common._resolve_thread", return_value="t-ZT"), \
         patch("juggle_cmd_agents_common._get_settings", return_value=_settings()):
        cmd_get_agent(_get_args())

    mock_mgr.spawn_agent.assert_called_once()
    update_calls = [c[0][0] for c in db.update_agent.call_args_list]
    assert "agent-busy" not in update_calls, (
        f"Busy agent was reassigned! calls: {db.update_agent.call_args_list}"
    )


def test_cmd_get_agent_cas_prevents_double_assign():
    """cmd_get_agent spawns fresh when CAS assign fails (TOCTOU guard).

    2026-06-15: two concurrent get-agent calls could both read the same
    idle agent; the loser's CAS returns False → must spawn fresh.
    """
    from juggle_cmd_agents_lifecycle import cmd_get_agent

    idle = {
        "id": "agent-idle", "pane_id": "%1", "role": "coder",
        "harness": "claude", "repo_path": "/repo",
        "status": "idle", "assigned_thread": None, "model": None,
    }
    spawned = {
        "id": "agent-new", "pane_id": "%2", "role": "coder",
        "harness": "claude", "repo_path": "/repo",
        "status": "idle", "assigned_thread": None, "model": None,
    }
    db = MagicMock()
    db.get_all_agents.return_value = [idle]
    db.get_ranked_idle_agents.return_value = [idle]
    db.get_thread.return_value = {
        "id": "t-ZT", "user_label": "ZT", "worktree_path": "",
        "worktree_branch": "", "main_repo_path": "", "open_questions": "[]",
    }
    db.cas_assign_agent.return_value = False  # CAS fails — agent already taken

    mock_cls = MagicMock()
    mock_mgr = mock_cls.return_value
    mock_mgr.wait_for_ready_to_paste.return_value = True
    mock_mgr.spawn_agent.return_value = spawned

    with patch("juggle_cmd_agents_common.get_db", return_value=db), \
         patch("juggle_tmux.JuggleTmuxManager", mock_cls), \
         patch("juggle_cmd_agents_common._resolve_thread", return_value="t-ZT"), \
         patch("juggle_cmd_agents_common._get_settings", return_value=_settings()):
        cmd_get_agent(_get_args())

    mock_mgr.spawn_agent.assert_called_once()
    update_calls = [c[0][0] for c in db.update_agent.call_args_list]
    assert "agent-idle" not in update_calls, (
        f"idle agent was directly assigned bypassing CAS! calls: {update_calls}"
    )
