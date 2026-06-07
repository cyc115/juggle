"""TDD tests for Bug1 (auto-create repo resolution) and Bug2 (harness-aware reuse)."""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _settings(harness="claude"):
    return {
        "agent": {
            "harness": harness,
            "harnesses": {
                "claude": {"type": "claude"},
                "reasonix": {"type": "template", "command": "reasonix run"},
            },
        }
    }


def _agent(agent_id="agent-1", pane_id="%1", harness="claude", role="coder",
           repo_path="/repo"):
    return {
        "id": agent_id,
        "pane_id": pane_id,
        "role": role,
        "harness": harness,
        "repo_path": repo_path,
        "status": "idle",
        "assigned_thread": None,
        "model": None,
    }


def _mock_db(agents=None, thread=None):
    db = MagicMock()
    db.get_all_agents.return_value = agents or []
    db.get_ranked_idle_agents.return_value = agents or []
    t = thread or {
        "id": "t-uuid",
        "user_label": "AB",
        "worktree_path": "",
        "worktree_branch": "",
        "main_repo_path": "",
        "open_questions": "[]",
    }
    db.get_thread.return_value = t
    return db


def _get_args(**kw):
    args = MagicMock()
    args.thread_id = kw.get("thread_id", "AB")
    args.role = kw.get("role", "coder")
    args.repo = kw.get("repo", None)
    args.harness = kw.get("harness", None)
    args.fresh = kw.get("fresh", False)
    args.model = kw.get("model", None)
    return args


# ── Bug 1: auto-create uses agent.repo_path, not cwd ────────────────────────

def test_get_agent_repo_flag_persists_repo_path(tmp_path):
    """get-agent --repo <path> must persist repo_path on the agent row."""
    from juggle_cmd_agents import cmd_get_agent

    repo_path = str(tmp_path / "myrepo")
    Path(repo_path).mkdir()

    spawned = _agent(repo_path=repo_path)
    db = _mock_db(agents=[])

    mock_cls = MagicMock()
    mock_mgr = mock_cls.return_value
    mock_mgr.spawn_agent.return_value = spawned

    with patch("juggle_cmd_agents.get_db", return_value=db), \
         patch("juggle_tmux.JuggleTmuxManager", mock_cls), \
         patch("juggle_cmd_agents._resolve_thread", return_value="t-uuid"), \
         patch("juggle_cmd_agents._get_settings", return_value=_settings()):
        cmd_get_agent(_get_args(repo=repo_path))

    update_kwargs = db.update_agent.call_args[1]
    assert update_kwargs.get("repo_path") == repo_path, (
        f"repo_path not persisted on agent row; got {update_kwargs}"
    )


def test_send_task_auto_create_uses_agent_repo_not_cwd(tmp_path):
    """Auto-create worktree uses agent.repo_path, not os.getcwd()."""
    from juggle_cmd_agents import cmd_send_task

    target_repo = tmp_path / "target-repo"
    target_repo.mkdir()

    agent = {
        "id": "agent-1", "pane_id": "%1", "role": "coder", "harness": "claude",
        "repo_path": str(target_repo), "status": "busy", "assigned_thread": "t-uuid",
    }
    thread = {
        "id": "t-uuid", "user_label": "AB",
        "worktree_path": "", "worktree_branch": "", "main_repo_path": "",
        "open_questions": "[]",
    }

    db = MagicMock()
    db.get_agent.return_value = agent
    db.get_thread.return_value = thread

    create_calls = []

    def fake_create(repo, label, worktree_root="/tmp"):
        create_calls.append(repo)
        return False, "", "", "mock"

    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("do the thing")

    args = MagicMock()
    args.agent_id = "agent-1"
    args.prompt_file = str(prompt_file)
    args.allow_main = False
    args.worktree_path = None
    args.worktree_branch = None
    args.main_repo_path = None

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    original_cwd = os.getcwd()
    os.chdir(str(vault_dir))

    try:
        with patch("juggle_cmd_agents.get_db", return_value=db), \
             patch("juggle_cmd_agents.JuggleTmuxManager") as mock_tmux, \
             patch("juggle_cmd_agents.get_adapter") as mock_adapter_fn, \
             patch("juggle_cmd_agents._create_worktree", side_effect=fake_create):
            mock_tmux.return_value.verify_pane.return_value = True
            mock_adapter = MagicMock()
            mock_adapter.is_interactive = True
            mock_adapter.decorate_task = lambda role, prompt: prompt
            mock_adapter_fn.return_value = mock_adapter

            with pytest.raises(SystemExit):
                cmd_send_task(args)
    finally:
        os.chdir(original_cwd)

    assert create_calls, "_create_worktree was never called"
    assert create_calls[0] == str(target_repo), (
        f"Expected {target_repo}, got {create_calls[0]!r}"
    )


# ── Bug 2: harness-aware reuse ────────────────────────────────────────────────

def test_get_agent_reuses_matching_harness_agent():
    """Idle agent with matching harness is reused (no fresh spawn)."""
    from juggle_cmd_agents import cmd_get_agent

    idle = _agent(harness="claude", repo_path="/repo")
    db = _mock_db(agents=[idle])

    mock_cls = MagicMock()
    mock_mgr = mock_cls.return_value
    mock_mgr.wait_for_ready_to_paste.return_value = True

    with patch("juggle_cmd_agents.get_db", return_value=db), \
         patch("juggle_tmux.JuggleTmuxManager", mock_cls), \
         patch("juggle_cmd_agents._resolve_thread", return_value="t-uuid"), \
         patch("juggle_cmd_agents._get_settings", return_value=_settings("claude")):
        cmd_get_agent(_get_args(repo="/repo", harness=None))

    mock_mgr.spawn_agent.assert_not_called()
    assert db.update_agent.call_args[0][0] == "agent-1"


def test_get_agent_skips_mismatched_harness_spawns_fresh():
    """Idle agent with different harness is skipped; fresh agent is spawned."""
    from juggle_cmd_agents import cmd_get_agent

    idle = _agent(harness="reasonix", repo_path="/repo")  # mismatch
    spawned = _agent(agent_id="agent-2", harness="claude")
    db = _mock_db(agents=[idle])

    mock_cls = MagicMock()
    mock_mgr = mock_cls.return_value
    mock_mgr.wait_for_ready_to_paste.return_value = True
    mock_mgr.spawn_agent.return_value = spawned

    with patch("juggle_cmd_agents.get_db", return_value=db), \
         patch("juggle_tmux.JuggleTmuxManager", mock_cls), \
         patch("juggle_cmd_agents._resolve_thread", return_value="t-uuid"), \
         patch("juggle_cmd_agents._get_settings", return_value=_settings("claude")):
        cmd_get_agent(_get_args(repo="/repo", harness=None))

    mock_mgr.spawn_agent.assert_called_once()


def test_get_agent_fresh_flag_skips_reuse():
    """--fresh forces a new spawn even when a matching idle agent exists."""
    from juggle_cmd_agents import cmd_get_agent

    idle = _agent(harness="claude", repo_path="/repo")
    spawned = _agent(agent_id="agent-2")
    db = _mock_db(agents=[idle])

    mock_cls = MagicMock()
    mock_mgr = mock_cls.return_value
    mock_mgr.wait_for_ready_to_paste.return_value = True
    mock_mgr.spawn_agent.return_value = spawned

    with patch("juggle_cmd_agents.get_db", return_value=db), \
         patch("juggle_tmux.JuggleTmuxManager", mock_cls), \
         patch("juggle_cmd_agents._resolve_thread", return_value="t-uuid"), \
         patch("juggle_cmd_agents._get_settings", return_value=_settings("claude")):
        cmd_get_agent(_get_args(repo="/repo", fresh=True))

    mock_mgr.spawn_agent.assert_called_once()


def test_get_agent_harness_flag_selects_reasonix_agent():
    """--harness reasonix reuses reasonix agent, skips claude agent."""
    from juggle_cmd_agents import cmd_get_agent

    claude_idle = _agent(agent_id="agent-claude", harness="claude", repo_path="/repo")
    reasonix_idle = _agent(agent_id="agent-reasonix", harness="reasonix", repo_path="/repo")
    db = _mock_db(agents=[claude_idle, reasonix_idle])
    db.get_ranked_idle_agents.return_value = [claude_idle, reasonix_idle]

    mock_cls = MagicMock()
    mock_mgr = mock_cls.return_value
    mock_mgr.wait_for_ready_to_paste.return_value = True

    with patch("juggle_cmd_agents.get_db", return_value=db), \
         patch("juggle_tmux.JuggleTmuxManager", mock_cls), \
         patch("juggle_cmd_agents._resolve_thread", return_value="t-uuid"), \
         patch("juggle_cmd_agents._get_settings", return_value=_settings("claude")):
        cmd_get_agent(_get_args(repo="/repo", harness="reasonix"))

    mock_mgr.spawn_agent.assert_not_called()
    assert db.update_agent.call_args[0][0] == "agent-reasonix"


def test_get_agent_reuse_resets_pane_cwd():
    """When reusing an idle agent, a cd command is sent to reset pane cwd."""
    from juggle_cmd_agents import cmd_get_agent

    idle = _agent(harness="claude", repo_path="/some/repo")
    db = _mock_db(agents=[idle])

    mock_cls = MagicMock()
    mock_mgr = mock_cls.return_value
    mock_mgr.wait_for_ready_to_paste.return_value = True

    with patch("juggle_cmd_agents.get_db", return_value=db), \
         patch("juggle_tmux.JuggleTmuxManager", mock_cls), \
         patch("juggle_cmd_agents._resolve_thread", return_value="t-uuid"), \
         patch("juggle_cmd_agents._get_settings", return_value=_settings("claude")):
        cmd_get_agent(_get_args(repo="/some/repo"))

    # A cd command must be sent to reset the pane's cwd
    all_calls = mock_mgr._run_tmux.call_args_list
    cd_sent = any(
        len(c[0]) >= 3 and c[0][0] == "send-keys" and "cd" in str(c[0])
        for c in all_calls
    )
    assert cd_sent, f"No cd command sent; _run_tmux calls: {all_calls}"
