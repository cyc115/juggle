"""Tests for juggle_dispatch_core's worktree auto-create path routed through
the VCS seam (vcs-route-workspace: A1/A2 workspace lifecycle, ``send_task_to_agent``
end of the call chain into ``_create_worktree``/``create_workspace``).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB  # noqa: E402


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "myrepo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("one\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "first")
    return r


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "dispatch.db"))
    d.init_db()
    d.set_active(True)
    return d


def _fake_mgr():
    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    mgr.send_task.return_value = "mock_hash"
    mgr.run_task_oneshot.return_value = ("mock_hash", None)
    return mgr


def test_send_task_auto_creates_worktree_forked_at_repo_head(
    db, repo, tmp_path, monkeypatch
):
    """coder + repo → send_task_to_agent auto-creates a worktree via the
    routed create_workspace, forked at the source repo's current HEAD (H2
    latent behavior preserved verbatim in this topic)."""
    monkeypatch.setenv("JUGGLE_WORKTREE_ROOT", str(tmp_path / "wts"))
    (tmp_path / "wts").mkdir()

    from juggle_dispatch_core import send_task_to_agent
    import juggle_dispatch_core as _core
    monkeypatch.setattr(_core, "DEFAULT_WORKTREE_ROOT", str(tmp_path / "wts"))

    thread_id = db.create_thread("t1", session_id="")
    agent_id = db.create_agent("coder", "%fake", repo_path=str(repo))

    repo_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    send_task_to_agent(
        db, agent_id, thread_id, "do the thing", _mgr=_fake_mgr(),
    )

    thread = db.get_thread(thread_id)
    wt_path = thread["worktree_path"]
    assert wt_path and Path(wt_path).is_dir()
    assert thread["worktree_branch"]
    assert thread["main_repo_path"] == str(repo)

    wt_head = _git(Path(wt_path), "rev-parse", "HEAD").stdout.strip()
    assert wt_head == repo_head


def test_send_task_reuses_existing_worktree(db, repo, tmp_path, monkeypatch):
    """A thread with an existing worktree_path is not recreated."""
    monkeypatch.setenv("JUGGLE_WORKTREE_ROOT", str(tmp_path / "wts"))
    (tmp_path / "wts").mkdir()

    import juggle_dispatch_core as _core
    monkeypatch.setattr(_core, "DEFAULT_WORKTREE_ROOT", str(tmp_path / "wts"))
    from juggle_dispatch_core import send_task_to_agent

    thread_id = db.create_thread("t2", session_id="")
    agent_id = db.create_agent("coder", "%fake", repo_path=str(repo))

    existing_wt = tmp_path / "already-there"
    existing_wt.mkdir()
    db.update_thread(
        thread_id, worktree_path=str(existing_wt),
        worktree_branch="cyc_existing", main_repo_path=str(repo),
    )

    send_task_to_agent(db, agent_id, thread_id, "do it", _mgr=_fake_mgr())

    thread = db.get_thread(thread_id)
    assert thread["worktree_path"] == str(existing_wt)
