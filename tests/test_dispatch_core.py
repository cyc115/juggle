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


# ── T-fix-dispatch-plan-spec-provision: Source of truth section placement ─────


def test_send_task_places_source_of_truth_immediately_after_working_directory(
    db, repo, tmp_path, monkeypatch
):
    """A topic with plan_path/spec_path bound to the dispatch thread must surface
    a '## Source of truth (READ FIRST)' section right after '## Working
    Directory' — before the role template — so a coder reads it before anything
    else (2026-07-03 GAP: plan/spec references previously never reached dispatch
    prompts)."""
    from dbops import db_topics as tp

    monkeypatch.setenv("JUGGLE_WORKTREE_ROOT", str(tmp_path / "wts"))
    (tmp_path / "wts").mkdir(exist_ok=True)

    import juggle_dispatch_core as _core
    monkeypatch.setattr(_core, "DEFAULT_WORKTREE_ROOT", str(tmp_path / "wts"))
    from juggle_dispatch_core import send_task_to_agent

    thread_id = db.create_thread("t3", session_id="")
    agent_id = db.create_agent("coder", "%fake", repo_path=str(repo))

    tp.create_topic(db, topic_id="T-plan", project_id="INBOX", title="T")
    tp.set_topic_thread(db, "T-plan", thread_id)
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET plan_path=?, spec_path=? WHERE id='T-plan'",
            ("plan/2026-07-03-x.md", "docs/2026-07-03-x-spec.md"),
        )
        conn.commit()

    send_task_to_agent(db, agent_id, thread_id, "do the thing", _mgr=_fake_mgr())

    full_prompt = db.get_agent(agent_id)["last_task"]
    assert "## Working Directory" in full_prompt
    assert "## Source of truth (READ FIRST)" in full_prompt
    assert "plan/2026-07-03-x.md" in full_prompt
    wd_idx = full_prompt.index("## Working Directory")
    sot_idx = full_prompt.index("## Source of truth (READ FIRST)")
    role_idx = full_prompt.index("## Role: Coder")
    assert wd_idx < sot_idx < role_idx


def test_send_task_omits_source_of_truth_when_topic_has_no_plan_or_spec(
    db, repo, tmp_path, monkeypatch
):
    monkeypatch.setenv("JUGGLE_WORKTREE_ROOT", str(tmp_path / "wts"))
    (tmp_path / "wts").mkdir(exist_ok=True)

    import juggle_dispatch_core as _core
    monkeypatch.setattr(_core, "DEFAULT_WORKTREE_ROOT", str(tmp_path / "wts"))
    from juggle_dispatch_core import send_task_to_agent

    thread_id = db.create_thread("t4", session_id="")
    agent_id = db.create_agent("coder", "%fake", repo_path=str(repo))

    send_task_to_agent(db, agent_id, thread_id, "do the thing", _mgr=_fake_mgr())

    full_prompt = db.get_agent(agent_id)["last_task"]
    assert "## Source of truth (READ FIRST)" not in full_prompt


# ── T-fix-literal-finalize-line: literal thread id + CLI path in prompts ──────


def test_send_task_renders_finalize_commands_fully_literal(db, repo, tmp_path, monkeypatch):
    """Regression pin — incident DG (2026-07-03): a coder agent invoked
    `juggle agent complete "$THREAD"` (empty thread id) because no literal
    thread id reached its prompt. The dispatched prompt must bake in the
    actual thread label and the dispatching repo's CLI path, and must never
    contain a generic '<thread>'/'$THREAD' placeholder or the deprecated
    'complete-agent'/'fail-agent' spelling."""
    monkeypatch.setenv("JUGGLE_WORKTREE_ROOT", str(tmp_path / "wts"))
    (tmp_path / "wts").mkdir(exist_ok=True)

    import juggle_dispatch_core as _core
    monkeypatch.setattr(_core, "DEFAULT_WORKTREE_ROOT", str(tmp_path / "wts"))
    from juggle_dispatch_core import send_task_to_agent

    thread_id = db.create_thread("literal finalize", session_id="")
    thread_label = db.get_thread(thread_id)["user_label"]
    agent_id = db.create_agent("coder", "%fake", repo_path=str(repo))

    send_task_to_agent(db, agent_id, thread_id, "do the thing", _mgr=_fake_mgr())

    full_prompt = db.get_agent(agent_id)["last_task"]
    assert thread_label in full_prompt
    assert "juggle_cli.py" in full_prompt
    for placeholder in ("<thread>", "$THREAD", "<THREAD>", "complete-agent", "fail-agent"):
        assert placeholder not in full_prompt, f"leftover placeholder: {placeholder}"


# ── T-fix-topic-dead-letter: --topic must resolve dispatch thread ─────────────


def test_cmd_send_task_topic_resolves_thread_for_unbound_agent(
    db, repo, tmp_path, monkeypatch
):
    """Regression pin — incident 2026-07-22 dead-letter RCA: `send-task --topic
    <label>` on an agent with assigned_thread=None dispatched with
    thread_uuid=None, baking the literal '<thread-unresolved>' into the
    finalize line (juggle_dispatch_core.py:204-208,233) so the agent's
    `agent complete <thread-unresolved>` call resolved to no thread and
    dead-lettered. cmd_send_task must resolve --topic to the dispatch thread
    when the agent has no bound thread, so the finalize line carries the real
    label instead."""
    from argparse import Namespace
    from unittest.mock import patch
    from juggle_cmd_agents_tasks import cmd_send_task

    monkeypatch.setenv("JUGGLE_WORKTREE_ROOT", str(tmp_path / "wts"))
    (tmp_path / "wts").mkdir(exist_ok=True)

    import juggle_dispatch_core as _core
    monkeypatch.setattr(_core, "DEFAULT_WORKTREE_ROOT", str(tmp_path / "wts"))

    thread_id = db.create_thread("dead-letter topic", session_id="")
    thread_label = db.get_thread(thread_id)["user_label"]

    agent_id = db.create_agent("coder", "%fake", repo_path=str(repo))
    assert db.get_agent(agent_id)["assigned_thread"] is None

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("do the thing")

    args = Namespace(
        agent_id=agent_id, prompt_file=str(prompt_file),
        no_template=False, worktree_path=None, worktree_branch=None,
        main_repo_path=None, allow_main=False, topic=thread_label,
        prompt_version=None, db_path=str(db.db_path),
    )

    with patch("juggle_cmd_agents_common.JuggleTmuxManager", return_value=_fake_mgr()):
        cmd_send_task(args)

    full_prompt = db.get_agent(agent_id)["last_task"]
    assert thread_label in full_prompt
    assert "<thread-unresolved>" not in full_prompt
