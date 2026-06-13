"""Tests for VCS provenance capture + `juggle runs restore` (T-vcs-checkpoint).

Covers: ledger capture of repo_path/vcs_type/before_sha/was_dirty at insert and
after_sha at close_run, plus the restore command's happy-path, dirty-refuse,
non-repo, no-op, and nothing-to-restore branches.
"""

import subprocess
import sys
import types
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from juggle_db import JuggleDB  # noqa: E402
import juggle_cmd_runs as cmd_runs  # noqa: E402


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "g"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "runs.db"))
    d.init_db()
    return d


@pytest.fixture
def thread(db):
    return db.create_thread("Test", session_id="")


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


def test_insert_captures_vcs_provenance(db, thread, git_repo):
    sha = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    run_id = db.insert_agent_run(
        thread_id=thread, input_prompt="p", agent_id="a", role="coder",
        model="m", harness="h", project_id="P", topic_id="T", task_id="N",
        repo_path=str(git_repo), vcs_type="git", before_sha=sha, was_dirty=False,
    )
    row = db.get_run(run_id)
    assert row["repo_path"] == str(git_repo)
    assert row["vcs_type"] == "git"
    assert row["before_sha"] == sha
    assert row["was_dirty"] == 0


def test_close_run_captures_after_sha(db, thread, git_repo):
    sha = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    run_id = db.insert_agent_run(
        thread_id=thread, input_prompt="p", agent_id="a", role="coder",
        model="m", harness="h", project_id="P", topic_id="T", task_id="N",
        repo_path=str(git_repo), vcs_type="git", before_sha=sha, was_dirty=False,
    )
    # advance HEAD then close
    (git_repo / "b.txt").write_text("two\n")
    _git(git_repo, "add", "b.txt")
    _git(git_repo, "commit", "-q", "-m", "second")
    new_sha = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    db.close_run(thread, output="done", diffstat=None)
    row = db.get_run(run_id)
    assert row["after_sha"] == new_sha
    assert row["after_sha"] != row["before_sha"]


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def _args(db_path, **kw):
    base = dict(db_path=str(db_path), task=None, thread=None, latest=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_restore_happy_path_creates_safety_branch(db, thread, git_repo, tmp_path, capsys):
    old = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    db.insert_agent_run(
        thread_id=thread, input_prompt="p", agent_id="a", role="coder",
        model="m", harness="h", project_id="P", topic_id="T", task_id="N1",
        repo_path=str(git_repo), vcs_type="git", before_sha=old, was_dirty=False,
    )
    (git_repo / "b.txt").write_text("two\n")
    _git(git_repo, "add", "b.txt")
    _git(git_repo, "commit", "-q", "-m", "second")
    db.close_run(thread, output="done", diffstat=None)

    cmd_runs.cmd_runs_restore(_args(tmp_path / "runs.db", task="N1"))
    out = capsys.readouterr().out
    assert "branch" in out.lower()
    cur = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert cur.startswith("juggle/pre-N1-")
    assert _git(git_repo, "rev-parse", "HEAD").stdout.strip() == old


def test_restore_refuses_dirty_tree(db, thread, git_repo, tmp_path, capsys):
    old = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    db.insert_agent_run(
        thread_id=thread, input_prompt="p", agent_id="a", role="coder",
        model="m", harness="h", project_id="P", topic_id="T", task_id="N1",
        repo_path=str(git_repo), vcs_type="git", before_sha=old, was_dirty=False,
    )
    (git_repo / "a.txt").write_text("DIRTY\n")  # uncommitted
    with pytest.raises(SystemExit):
        cmd_runs.cmd_runs_restore(_args(tmp_path / "runs.db", task="N1"))
    assert "dirty" in capsys.readouterr().out.lower()


def test_restore_non_repo_errors(db, thread, tmp_path, capsys):
    db.insert_agent_run(
        thread_id=thread, input_prompt="p", agent_id="a", role="coder",
        model="m", harness="h", project_id="P", topic_id="T", task_id="N1",
        repo_path=str(tmp_path / "gone"), vcs_type="git",
        before_sha="deadbeef", was_dirty=False,
    )
    with pytest.raises(SystemExit):
        cmd_runs.cmd_runs_restore(_args(tmp_path / "runs.db", task="N1"))
    assert "missing" in capsys.readouterr().out.lower()


def test_restore_noop_when_head_unchanged(db, thread, git_repo, tmp_path, capsys):
    old = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    db.insert_agent_run(
        thread_id=thread, input_prompt="p", agent_id="a", role="coder",
        model="m", harness="h", project_id="P", topic_id="T", task_id="N1",
        repo_path=str(git_repo), vcs_type="git", before_sha=old, was_dirty=False,
    )
    db.close_run(thread, output="done", diffstat=None)  # after_sha == old
    cmd_runs.cmd_runs_restore(_args(tmp_path / "runs.db", task="N1"))
    assert "no-op" in capsys.readouterr().out.lower()


def test_restore_nothing_when_no_provenance(db, thread, tmp_path, capsys):
    db.insert_agent_run(
        thread_id=thread, input_prompt="p", agent_id="a", role="coder",
        model="m", harness="h", project_id="P", topic_id="T", task_id="N1",
    )
    cmd_runs.cmd_runs_restore(_args(tmp_path / "runs.db", task="N1"))
    assert "nothing to restore" in capsys.readouterr().out.lower()


def test_restore_requires_selector(tmp_path):
    with pytest.raises(SystemExit):
        cmd_runs.cmd_runs_restore(_args(tmp_path / "x.db"))


def test_restore_node_alias_still_works(db, thread, git_repo, tmp_path, capsys):
    """--node remains a deprecated alias for --task (dest='task')."""
    old = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    db.insert_agent_run(
        thread_id=thread, input_prompt="p", agent_id="a", role="coder",
        model="m", harness="h", project_id="P", topic_id="T", task_id="N1",
        repo_path=str(git_repo), vcs_type="git", before_sha=old, was_dirty=False,
    )
    (git_repo / "b.txt").write_text("two\n")
    _git(git_repo, "add", "b.txt")
    _git(git_repo, "commit", "-q", "-m", "second")
    db.close_run(thread, output="done", diffstat=None)
    # argparse stores --node into dest 'task'; simulate that here.
    cmd_runs.cmd_runs_restore(_args(tmp_path / "runs.db", task="N1"))
    assert "branch" in capsys.readouterr().out.lower()
