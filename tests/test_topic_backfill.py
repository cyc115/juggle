"""One-time backfill of the stale-open conversation pile
(2026-06-30 topic-graph-state-unify F6).

CLOSES-ONLY on a provable merge of cyc_<user_label> to main; unmerged / no-branch
topics are left open; no children are fabricated.
"""
import subprocess

import juggle_topic_reconcile as tr


def _git(repo, *a):
    return subprocess.run(
        ["git", "-C", str(repo), *a], check=True, capture_output=True, text=True
    )


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _add_branch(repo, branch, *, merged):
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / f"{branch}.txt").write_text("work\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", f"work {branch}")
    if merged:
        _git(repo, "checkout", "-q", "main")
        _git(repo, "merge", "-q", "--no-ff", "-m", f"merge {branch}", branch)
    _git(repo, "checkout", "-q", "main")


def _stale_open_topic(db, repo, *, label_branch_merged, title="feature"):
    tid = db.create_thread(topic=title, session_id="s")
    db.add_message(tid, role="user", content="build the thing")
    label = db.get_thread(tid)["user_label"]
    _add_branch(repo, f"cyc_{label}", merged=label_branch_merged)
    db.update_thread(tid, main_repo_path=str(repo))
    return tid


def test_backfill_closes_merged_leaves_unmerged(juggle_db, tmp_path):
    """2026-06-30 unify backfill: merged cyc_<label> -> closed; unmerged -> stays open."""
    repo = _make_repo(tmp_path)
    merged = _stale_open_topic(
        juggle_db, repo, label_branch_merged=True, title="login page redesign"
    )
    unmerged = _stale_open_topic(
        juggle_db, repo, label_branch_merged=False, title="payment webhook retries"
    )
    closed = tr.backfill_stale_open_topics(juggle_db)
    assert merged in closed and unmerged not in closed
    assert juggle_db.get_thread(merged)["state"] == "done"
    assert juggle_db.get_thread(unmerged)["state"] == "open"


def test_backfill_leaves_topic_with_children(juggle_db, tmp_path):
    """2026-06-30 unify backfill: a topic WITH children is out of scope (reconciler owns it)."""
    from dbops import db_graph

    repo = _make_repo(tmp_path)
    tid = _stale_open_topic(juggle_db, repo, label_branch_merged=True)
    db_graph.create_task(juggle_db, task_id="c1", project_id="INBOX", title="t", prompt="p")
    db_graph.set_task_topic(juggle_db, "c1", tid)
    closed = tr.backfill_stale_open_topics(juggle_db)
    assert tid not in closed
    assert juggle_db.get_thread(tid)["state"] == "open"
