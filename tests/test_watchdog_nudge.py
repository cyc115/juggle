import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ── commits_behind_trunk (real git repo) ────────────────────────────────────

def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    _git(repo, "branch", "-M", "main")
    return str(repo)


def test_commits_behind_trunk_counts_commits_since_fork(tmp_path):
    from juggle_watchdog_rebase_nudge import commits_behind_trunk

    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-b", "cyc_X")
    (Path(repo) / "b.py").write_text("y = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "branch work")
    _git(repo, "checkout", "main")
    for i in range(6):
        (Path(repo) / f"m{i}.py").write_text(f"z = {i}\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", f"main commit {i}")

    n = commits_behind_trunk(repo, "cyc_X", "main")
    assert n == 6


def test_commits_behind_trunk_zero_when_branch_is_current(tmp_path):
    from juggle_watchdog_rebase_nudge import commits_behind_trunk
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-b", "cyc_Y")
    n = commits_behind_trunk(repo, "cyc_Y", "main")
    assert n == 0


def test_commits_behind_trunk_none_on_missing_branch(tmp_path):
    from juggle_watchdog_rebase_nudge import commits_behind_trunk
    repo = _init_repo(tmp_path)
    n = commits_behind_trunk(repo, "cyc_nonexistent", "main")
    assert n is None


# ── sweep_rebase_nudges (real DB + real git repo, fake mgr) ─────────────────

def _make_running_topic_with_agent(db, repo, branch, thread_label="RN"):
    from dbops import db_topics

    tid = f"T-{thread_label}"
    db_topics.create_topic(db, topic_id=tid, project_id="INBOX", title="F")
    thread_id = db.create_thread(f"[{tid}]", session_id="s")
    db.update_thread(thread_id, worktree_path=f"/tmp/wt-{thread_label}",
                      worktree_branch=branch, main_repo_path=repo)
    db_topics.set_topic_thread(db, tid, thread_id)
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET state = 'running' WHERE id = ?", (tid,))
        conn.commit()
    agent_id = db.create_agent("coder", pane_id="%1")
    db.update_agent(agent_id, assigned_thread=thread_id, status="busy")
    return tid, thread_id, agent_id


def test_sweep_nudges_topic_when_trunk_moved_past_threshold(tmp_path):
    from juggle_db import JuggleDB
    from juggle_watchdog_rebase_nudge import sweep_rebase_nudges

    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-b", "cyc_RN")
    _git(repo, "checkout", "main")
    for i in range(5):
        (Path(repo) / f"m{i}.py").write_text(f"z = {i}\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", f"main commit {i}")

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    tid, thread_id, agent_id = _make_running_topic_with_agent(db, repo, "cyc_RN")

    mgr = Mock()
    nudged = sweep_rebase_nudges(db, mgr)

    assert nudged == [tid]
    mgr.send_message.assert_called_once()
    pane_arg, text_arg = mgr.send_message.call_args[0]
    assert pane_arg == "%1"
    assert "5 commits" in text_arg


def test_sweep_does_not_nudge_below_threshold(tmp_path):
    from juggle_db import JuggleDB
    from juggle_watchdog_rebase_nudge import sweep_rebase_nudges

    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-b", "cyc_RN2")
    _git(repo, "checkout", "main")
    (Path(repo) / "m0.py").write_text("z = 0\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "main commit 0")

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    _make_running_topic_with_agent(db, repo, "cyc_RN2")

    mgr = Mock()
    nudged = sweep_rebase_nudges(db, mgr)

    assert nudged == []
    mgr.send_message.assert_not_called()


def test_sweep_nudges_at_most_once_per_running_episode(tmp_path):
    """Pin: single-nudge-only — a second sweep tick does not re-nudge."""
    from juggle_db import JuggleDB
    from juggle_watchdog_rebase_nudge import sweep_rebase_nudges

    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-b", "cyc_RN3")
    _git(repo, "checkout", "main")
    for i in range(5):
        (Path(repo) / f"m{i}.py").write_text(f"z = {i}\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", f"main commit {i}")

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    tid, _, _ = _make_running_topic_with_agent(db, repo, "cyc_RN3")

    mgr = Mock()
    first = sweep_rebase_nudges(db, mgr)
    second = sweep_rebase_nudges(db, mgr)

    assert first == [tid]
    assert second == []
    mgr.send_message.assert_called_once()


def test_clear_rebase_nudge_allows_a_fresh_nudge_next_episode(tmp_path):
    from juggle_db import JuggleDB
    from juggle_watchdog_rebase_nudge import sweep_rebase_nudges, clear_rebase_nudge

    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-b", "cyc_RN4")
    _git(repo, "checkout", "main")
    for i in range(5):
        (Path(repo) / f"m{i}.py").write_text(f"z = {i}\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", f"main commit {i}")

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    tid, _, _ = _make_running_topic_with_agent(db, repo, "cyc_RN4")

    mgr = Mock()
    sweep_rebase_nudges(db, mgr)
    clear_rebase_nudge(db, tid)
    second = sweep_rebase_nudges(db, mgr)

    assert second == [tid]
    assert mgr.send_message.call_count == 2


def test_sweep_skips_topic_with_no_busy_agent(tmp_path):
    from juggle_db import JuggleDB
    from juggle_watchdog_rebase_nudge import sweep_rebase_nudges

    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-b", "cyc_RN5")
    _git(repo, "checkout", "main")
    for i in range(5):
        (Path(repo) / f"m{i}.py").write_text(f"z = {i}\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", f"main commit {i}")

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    from dbops import db_topics
    tid = db_topics.create_topic(db, topic_id="T-noagent", project_id="INBOX", title="F")
    thread_id = db.create_thread(f"[{tid}]", session_id="s")
    db.update_thread(thread_id, worktree_path="/tmp/wt-noagent",
                      worktree_branch="cyc_RN5", main_repo_path=repo)
    db_topics.set_topic_thread(db, tid, thread_id)
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET state = 'running' WHERE id = ?", (tid,))
        conn.commit()

    mgr = Mock()
    nudged = sweep_rebase_nudges(db, mgr)

    assert nudged == []
    mgr.send_message.assert_not_called()
