"""TDD: verified ⟺ recorded merge-commit (T-verified-merged-sha).

DEFECT (core invariant): 'verified ⟺ merged to main' was unreliable — false
-verified recurred 3× through different holes (empty-branch fail-open, branch
-gone fail-open, _orphan_recoverable bypassing the merge guard). This pins the
single source of truth: a topic verifies IFF it has a recorded ``merged_sha``
that is an ancestor of ``main``. Nothing else.

Temp DBs only; isolated git repos under tmp_path. No prod DB, no watchdog.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as t  # noqa: E402
from dbops import db_topics_reconcile as tr  # noqa: E402
import juggle_graph_dispatch as gd  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _main_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "main").stdout.strip()


def _unmerged_sha(repo: Path) -> str:
    """A commit on a side branch that is NOT an ancestor of main."""
    _git(repo, "checkout", "-q", "-b", "cyc_side")
    (repo / "f.txt").write_text("side\n")
    _git(repo, "commit", "-aqm", "side")
    sha = _git(repo, "rev-parse", "cyc_side").stdout.strip()
    _git(repo, "checkout", "-q", "main")
    return sha


def _mk_project(db, pid="INBOX"):
    if pid != "INBOX":
        db.create_project(pid, pid, "test")
    return pid


def _bind(db, topic_id, repo: Path):
    """Bind a topic to a thread on ``repo`` and walk it up to 'integrating'."""
    tid = db.create_thread(topic="work", session_id="s")
    db.update_thread(tid, worktree_branch="cyc_x", main_repo_path=str(repo))
    t.create_topic(db, topic_id=topic_id, project_id="INBOX", title="Feat")
    t.set_topic_thread(db, topic_id, tid)
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start"):
        t.topic_transition(db, topic_id, ev)
    return tid


# ---------------------------------------------------------------------------
# topic_transition gate
# ---------------------------------------------------------------------------


def test_verify_refused_when_merged_sha_null(db, tmp_path):
    repo = _make_repo(tmp_path)
    _bind(db, "T-feat", repo)  # merged_sha stays NULL

    with pytest.raises(t.UnmergedVerifyRefused):
        t.topic_transition(db, "T-feat", "integrate_ok")
    assert t.get_topic(db, "T-feat")["state"] == "integrating"


def test_verify_allowed_when_merged_sha_is_ancestor(db, tmp_path):
    repo = _make_repo(tmp_path)
    _bind(db, "T-feat", repo)
    t.set_topic_merged_sha(db, "T-feat", _main_sha(repo))  # trivially on main

    assert t.topic_transition(db, "T-feat", "integrate_ok") == "verified"


def test_verify_refused_when_merged_sha_not_on_main(db, tmp_path):
    repo = _make_repo(tmp_path)
    _bind(db, "T-feat", repo)
    t.set_topic_merged_sha(db, "T-feat", _unmerged_sha(repo))  # side branch

    with pytest.raises(t.UnmergedVerifyRefused):
        t.topic_transition(db, "T-feat", "integrate_ok")


# ---------------------------------------------------------------------------
# reconcile gate — closes the _orphan_recoverable bypass
# ---------------------------------------------------------------------------


def _mk_topic_all_verified(db, topic_id, repo: Path | None, *, bind=True):
    t.create_topic(db, topic_id=topic_id, project_id="INBOX", title=topic_id)
    with db._connect() as conn:
        conn.execute("UPDATE graph_topics SET state='integrating' WHERE id=?", (topic_id,))
        for i in range(2):
            nid = f"{topic_id}-k{i}"
            conn.execute(
                "INSERT INTO graph_tasks(id,project_id,title,prompt,state,topic_id,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,datetime('now'),datetime('now'))",
                (nid, "INBOX", nid, "p", "verified", topic_id),
            )
        conn.commit()
    if bind and repo is not None:
        tid = db.create_thread(topic="w", session_id="s")
        db.update_thread(tid, worktree_branch="cyc_x", main_repo_path=str(repo))
        t.set_topic_thread(db, topic_id, tid)


def test_reconcile_does_not_verify_null_merged_sha_even_orphaned(db, tmp_path):
    """Orphan (thread/branch gone) with merged_sha NULL must NEVER verify."""
    _mk_topic_all_verified(db, "T1", repo=None, bind=False)  # thread_id NULL

    result = tr.reconcile_topic_state(db, "T1")

    assert result == "integrating"
    assert t.get_topic(db, "T1")["state"] == "integrating"
    assert t.get_topic(db, "T1")["verified_at"] is None


def test_reconcile_verifies_when_merged_sha_ancestor(db, tmp_path):
    repo = _make_repo(tmp_path)
    _mk_topic_all_verified(db, "T1", repo)
    t.set_topic_merged_sha(db, "T1", _main_sha(repo))

    result = tr.reconcile_topic_state(db, "T1")

    assert result == "verified"
    assert t.get_topic(db, "T1")["verified_at"] is not None


def test_reconcile_does_not_verify_sha_not_on_main(db, tmp_path):
    repo = _make_repo(tmp_path)
    _mk_topic_all_verified(db, "T1", repo)
    t.set_topic_merged_sha(db, "T1", _unmerged_sha(repo))

    assert tr.reconcile_topic_state(db, "T1") == "integrating"


# ---------------------------------------------------------------------------
# worktree_branch recorded at dispatch (hole #3)
# ---------------------------------------------------------------------------


def test_worktree_branch_recorded_at_dispatch(db):
    t.create_topic(db, topic_id="a", project_id="INBOX", title="Topic a")
    nid = "a-k0"
    g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt="p")
    with db._connect() as conn:
        conn.execute("UPDATE graph_tasks SET topic_id='a' WHERE id=?", (nid,))
        conn.commit()
    t.recompute_topic_ready(db, "INBOX")
    db.set_setting(gd.ARMED_PROJECT_KEY, "INBOX")

    calls = []
    gd.graph_tick(db, dispatch_fn=lambda *a, **k: calls.append(a))

    topic = t.get_topic(db, "a")
    assert topic["thread_id"]
    thread = db.get_thread(topic["thread_id"])
    assert (thread.get("worktree_branch") or "").strip(), \
        "worktree_branch must be recorded at dispatch"
    assert thread["worktree_branch"].startswith("cyc_")
