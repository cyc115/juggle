import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    for cmd in [
        ["git", "init", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        ["git", "-C", str(repo), "config", "user.name", "T"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True, capture_output=True)
    return str(repo)


# ── _create_worktree ──────────────────────────────────────────────────────────

def test_create_worktree_creates_linked_worktree(git_repo, tmp_path):
    from juggle_cmd_agents import _create_worktree
    ok, wt_path, branch, msg = _create_worktree(git_repo, "AB", worktree_root=str(tmp_path))
    assert ok, msg
    assert Path(wt_path).is_dir()
    assert branch == "cyc_AB"
    wt_list = subprocess.run(
        ["git", "-C", git_repo, "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    ).stdout
    assert wt_path in wt_list


def test_create_worktree_branch_starts_from_repo_head(git_repo, tmp_path):
    from juggle_cmd_agents import _create_worktree
    repo_head = subprocess.run(
        ["git", "-C", git_repo, "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    ok, wt_path, branch, _ = _create_worktree(git_repo, "AB", worktree_root=str(tmp_path))
    assert ok
    wt_head = subprocess.run(
        ["git", "-C", wt_path, "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert wt_head == repo_head


def test_create_worktree_symlinks_venv(git_repo, tmp_path):
    from juggle_cmd_agents import _create_worktree
    (Path(git_repo) / ".venv").mkdir()
    ok, wt_path, branch, _ = _create_worktree(git_repo, "CD", worktree_root=str(tmp_path))
    assert ok
    venv_link = Path(wt_path) / ".venv"
    assert venv_link.is_symlink()
    assert venv_link.resolve() == (Path(git_repo) / ".venv").resolve()


def test_create_worktree_no_venv_skips_silently(git_repo, tmp_path):
    from juggle_cmd_agents import _create_worktree
    assert not (Path(git_repo) / ".venv").exists()
    ok, wt_path, branch, _ = _create_worktree(git_repo, "EF", worktree_root=str(tmp_path))
    assert ok
    assert not (Path(wt_path) / ".venv").exists()


def test_create_worktree_idempotent(git_repo, tmp_path):
    from juggle_cmd_agents import _create_worktree
    _create_worktree(git_repo, "GH", worktree_root=str(tmp_path))
    ok, wt_path, branch, msg = _create_worktree(git_repo, "GH", worktree_root=str(tmp_path))
    assert ok
    assert "already exists" in msg


# ── cmd_send_task auto-create + guard tests ───────────────────────────────────

def _minimal_send_task_args(prompt_file: str, role: str, repo_path: str,
                             allow_main: bool = False) -> object:
    args = Mock()
    args.agent_id = "aabbccdd-1234"
    args.prompt_file = prompt_file
    args.no_template = True
    args.allow_main = allow_main
    args.worktree_path = None
    args.worktree_branch = None
    args.main_repo_path = None
    return args


def _minimal_agent(role: str, repo_path: str) -> dict:
    return {
        "id": "aabbccdd-1234",
        "pane_id": "juggle:0.1",
        "role": role,
        "repo_path": repo_path,
        "assigned_thread": "thread-uuid-1",
        "model": None,
        "harness": "claude",
        "oneshot_pid": None,
    }


def _minimal_thread(worktree_path=None) -> dict:
    return {
        "id": "thread-uuid-1",
        "user_label": "AB",
        "worktree_path": worktree_path,
        "worktree_branch": None,
        "main_repo_path": None,
    }


def _run_send_task(args, agent, thread, create_worktree_result=None, git_repo_path=None):
    """Drive cmd_send_task with mocked DB and tmux/adapter."""
    import juggle_cmd_agents as _mod

    with patch("juggle_cmd_agents.get_db") as mock_get_db:
        db = Mock()
        db.get_agent.return_value = agent
        db.get_thread.return_value = thread
        db.update_thread = Mock()
        db.update_agent = Mock()
        mock_get_db.return_value = db
        with patch("juggle_cmd_agents.JuggleTmuxManager") as MockMgr:
            MockMgr.return_value.verify_pane.return_value = True
            MockMgr.return_value.send_task.return_value = "hash123"
            with patch("juggle_cmd_agents.get_adapter") as mock_adapter:
                mock_adapter.return_value.is_interactive = True
                mock_adapter.return_value.decorate_task = lambda role, p: p
                mock_adapter.return_value._cfg = {}
                with patch("juggle_cmd_agents._get_settings", return_value={
                    "agent": {"quality_gate_skill": ""},
                    "task_templates": {},
                }):
                    if create_worktree_result is not None:
                        with patch("juggle_cmd_agents._create_worktree",
                                   return_value=create_worktree_result):
                            _mod.cmd_send_task(args)
                    else:
                        _mod.cmd_send_task(args)
    return db


def test_auto_create_triggered_for_coder_with_repo(git_repo, tmp_path):
    """coder + repo_path → _create_worktree called, worktree fields persisted to thread."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("do stuff")
    args = _minimal_send_task_args(str(prompt_file), "coder", git_repo)
    agent = _minimal_agent("coder", git_repo)
    thread = _minimal_thread()

    wt_path = str(tmp_path / "juggle-repo-AB")
    fake_result = (True, wt_path, "cyc_AB", "Worktree created")

    db = _run_send_task(args, agent, thread, create_worktree_result=fake_result)

    # update_thread must be called with the new worktree fields
    calls = [str(c) for c in db.update_thread.call_args_list]
    assert any("cyc_AB" in c for c in calls), f"Expected worktree_branch in calls: {calls}"


def test_auto_create_not_triggered_for_researcher(git_repo, tmp_path):
    """researcher role is exempt from auto-create."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("research this")
    args = _minimal_send_task_args(str(prompt_file), "researcher", git_repo)
    agent = _minimal_agent("researcher", git_repo)
    thread = _minimal_thread()

    with patch("juggle_cmd_agents.get_db") as mock_get_db:
        db = Mock()
        db.get_agent.return_value = _minimal_agent("researcher", git_repo)
        db.get_thread.return_value = _minimal_thread()
        db.update_thread = Mock()
        db.update_agent = Mock()
        mock_get_db.return_value = db
        with patch("juggle_cmd_agents._create_worktree") as mock_create:
            with patch("juggle_cmd_agents.JuggleTmuxManager") as MockMgr:
                MockMgr.return_value.verify_pane.return_value = True
                MockMgr.return_value.send_task.return_value = "hash"
                with patch("juggle_cmd_agents.get_adapter") as mock_adapter:
                    mock_adapter.return_value.is_interactive = True
                    mock_adapter.return_value.decorate_task = lambda r, p: p
                    mock_adapter.return_value._cfg = {}
                    with patch("juggle_cmd_agents._get_settings", return_value={
                        "agent": {"quality_gate_skill": ""},
                        "task_templates": {},
                    }):
                        from juggle_cmd_agents import cmd_send_task
                        cmd_send_task(args)
    mock_create.assert_not_called()


def test_guard_exits_nonzero_when_create_fails(tmp_path):
    """coder + repo_path + create fails + no --allow-main → exit(1)."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("do stuff")
    args = _minimal_send_task_args(str(prompt_file), "coder", "/fake/repo")
    agent = _minimal_agent("coder", "/fake/repo")
    thread = _minimal_thread()

    with pytest.raises(SystemExit) as exc:
        _run_send_task(args, agent, thread,
                       create_worktree_result=(False, "", "", "git error"))
    assert exc.value.code == 1


def test_allow_main_bypasses_guard(tmp_path):
    """--allow-main lets coder dispatch even when create fails."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("do stuff")
    args = _minimal_send_task_args(str(prompt_file), "coder", "/fake/repo", allow_main=True)
    agent = _minimal_agent("coder", "/fake/repo")
    thread = _minimal_thread()

    # Should NOT raise SystemExit
    _run_send_task(args, agent, thread,
                   create_worktree_result=(False, "", "", "git error"))


def test_worktree_preamble_injected_into_prompt(tmp_path):
    """When worktree is set on thread, cd preamble appears in sent prompt."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("implement feature X")
    wt_path = str(tmp_path / "juggle-repo-AB")
    args = _minimal_send_task_args(str(prompt_file), "coder", "/fake/repo")
    agent = _minimal_agent("coder", "/fake/repo")
    thread = _minimal_thread(worktree_path=wt_path)
    thread["worktree_branch"] = "cyc_AB"

    sent_prompts = []

    with patch("juggle_cmd_agents.get_db") as mock_get_db:
        db = Mock()
        db.get_agent.return_value = agent
        db.get_thread.return_value = thread
        db.update_thread = Mock()
        db.update_agent = Mock()
        mock_get_db.return_value = db
        with patch("juggle_cmd_agents.JuggleTmuxManager") as MockMgr:
            MockMgr.return_value.verify_pane.return_value = True
            MockMgr.return_value.send_task.side_effect = lambda pane, prompt, **kw: (
                sent_prompts.append(prompt) or "hash"
            )
            with patch("juggle_cmd_agents.get_adapter") as mock_adapter:
                mock_adapter.return_value.is_interactive = True
                mock_adapter.return_value.decorate_task = lambda r, p: p
                mock_adapter.return_value._cfg = {}
                with patch("juggle_cmd_agents._get_settings", return_value={
                    "agent": {"quality_gate_skill": ""},
                    "task_templates": {},
                }):
                    with patch("juggle_cmd_agents._create_worktree",
                               return_value=(True, wt_path, "cyc_AB", "exists")):
                        from juggle_cmd_agents import cmd_send_task
                        cmd_send_task(args)

    assert sent_prompts, "send_task was not called"
    assert wt_path in sent_prompts[0], "Worktree path missing from prompt"
    assert "cd" in sent_prompts[0].lower()
