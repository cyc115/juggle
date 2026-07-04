"""Heal landed topics whose branch was GC'd (T-fix-heal-deleted-branch).

INCIDENT (2026-07-04 integrate-wedge #2 follow-up; two heal blind spots):

GAP 1 (deleted branch): ``_heal_merged_sha`` resolved a landed sha only from the
branch ref (``resolve_landed_sha``) or a ``pending_merged_sha`` breadcrumb. Once
integrate deleted / GC'd the ``cyc_*`` branch and no pending breadcrumb was
stashed, heal had nothing to prove the merge with — the topic wedged
'integrating' forever even though its commit was on main. Observed on
T-rca-spool-deadletter (commit fabebc7 on main, cyc_AR GC'd; hand-stamped as an
interim fix). FIX: fall back to the topic's recorded head/``submitted_rev`` and
stamp merged_sha IFF that commit is provably an ancestor of main; else leave the
state (fail-closed).

GAP 2 (failed-integration not lifted): reconcile only ever transitioned
'integrating' topics. A topic parked in 'failed-integration' whose branch
SUBSEQUENTLY landed (operator or repair re-ran integrate) kept its failed state
even with merged_sha stamped — dependents stayed blocked though the work was on
main. Observed on T-gl-learn-cmd (merged_sha 1167e457 stamped, state stayed
failed-integration; hand-flipped to verified as an interim fix). FIX: reconcile
lifts 'failed-integration' → verified when the merge is provable, clears the
fail_envelope, and unblocks dependents.

Temp DBs + isolated git repos only. No prod DB, no watchdog.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as t  # noqa: E402
from dbops import db_topics_reconcile as tr  # noqa: E402


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


def _landed_then_deleted(repo: Path, branch: str) -> str:
    """Create ``branch``, commit, ff-merge into main, then DELETE the branch ref.
    Returns the commit sha — now an ancestor of main, but its branch is gone."""
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / "f.txt").write_text("feat\n")
    _git(repo, "commit", "-aqm", "feat")
    sha = _git(repo, "rev-parse", branch).stdout.strip()
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", branch)
    _git(repo, "branch", "-qD", branch)  # branch ref gone (GC'd)
    return sha


def _unmerged_then_deleted(repo: Path, branch: str) -> str:
    """Commit on ``branch``, NEVER merge it, then delete the branch ref.
    Returns the commit sha — NOT an ancestor of main; ref gone."""
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / "f.txt").write_text("side\n")
    _git(repo, "commit", "-aqm", "side")
    sha = _git(repo, "rev-parse", branch).stdout.strip()
    _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "-qD", branch)
    return sha


def _make_db(tmp_path: Path) -> JuggleDB:
    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    return db


def _mk_integrating_topic(db, topic_id, repo: Path, branch: str, *,
                          submitted_rev=None):
    """A topic at 'integrating', all member tasks verified, bound to a thread on
    ``repo`` whose (now-deleted) branch was ``branch``. merged_sha NULL."""
    t.create_topic(db, topic_id=topic_id, project_id="INBOX", title=topic_id)
    for i in range(2):
        nid = f"{topic_id}-k{i}"
        g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt="p")
        g.set_task_topic(db, nid, topic_id)
    tid = db.create_thread(topic="w", session_id="s")
    db.update_thread(tid, worktree_branch=branch, main_repo_path=str(repo))
    t.set_topic_thread(db, topic_id, tid)
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET state='integrating' WHERE id=? AND kind='topic'",
                     (topic_id,))
        for i in range(2):
            conn.execute("UPDATE nodes SET state='verified' WHERE id=?",
                         (f"{topic_id}-k{i}",))
        conn.commit()
    if submitted_rev is not None:
        t.set_topic_submitted_rev(db, topic_id, submitted_rev)


# ── GAP 1: branch deleted, proof only via the recorded head/submitted_rev ────


def test_heal_integrating_branch_deleted_commit_on_main(tmp_path):
    """Pin 1 (fix-heal-deleted-branch): integrating topic, its branch deleted
    (GC'd), no pending breadcrumb, but the recorded submitted_rev IS an ancestor
    of main → reconcile stamps merged_sha from it and heals to 'verified'."""
    repo = _make_repo(tmp_path)
    sha = _landed_then_deleted(repo, "cyc_gc")
    db = _make_db(tmp_path)
    _mk_integrating_topic(db, "T-gc", repo, "cyc_gc", submitted_rev=sha)
    assert t.get_topic(db, "T-gc")["merged_sha"] is None

    result = tr.reconcile_topic_state(db, "T-gc")

    assert result == "verified", "landed-but-branch-gone topic must heal to verified"
    healed = t.get_topic(db, "T-gc")
    assert healed["merged_sha"] == sha, "must record the sha it proved from git"
    assert healed["verified_at"] is not None


def test_heal_integrating_branch_deleted_commit_not_on_main(tmp_path):
    """Pin 2 (fix-heal-deleted-branch): integrating topic, branch deleted, and the
    recorded commit is NOT an ancestor of main → NO proof → state unchanged
    (fail-closed), merged_sha stays NULL."""
    repo = _make_repo(tmp_path)
    sha = _unmerged_then_deleted(repo, "cyc_gc2")
    db = _make_db(tmp_path)
    _mk_integrating_topic(db, "T-gc2", repo, "cyc_gc2", submitted_rev=sha)

    assert tr.reconcile_topic_state(db, "T-gc2") == "integrating"
    assert t.get_topic(db, "T-gc2")["merged_sha"] is None


# ── GAP 2: failed-integration lifted once the branch lands ───────────────────


def _mk_failed_integration_topic(db, topic_id, *, merged_sha, fail_envelope):
    t.create_topic(db, topic_id=topic_id, project_id="INBOX", title=topic_id)
    nid = f"{topic_id}-k0"
    g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt="p")
    g.set_task_topic(db, nid, topic_id)
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET state='verified' WHERE id=?", (nid,))
        conn.execute(
            "UPDATE nodes SET state='failed-integration', merged_sha=?, "
            "fail_envelope=? WHERE id=? AND kind='topic'",
            (merged_sha, fail_envelope, topic_id),
        )
        conn.commit()


def test_failed_integration_lifted_when_merged_sha_present(tmp_path):
    """Pin 3 (fix-heal-deleted-branch GAP 2): a 'failed-integration' topic whose
    branch subsequently landed (merged_sha stamped + on main) must lift to
    'verified', clear its fail_envelope, and unblock a blocked-failed dependent."""
    repo = _make_repo(tmp_path)
    main_sha = _git(repo, "rev-parse", "main").stdout.strip()
    db = _make_db(tmp_path)

    # A: failed-integration but merged_sha is on main; bind a thread for repo.
    _mk_failed_integration_topic(db, "T-A", merged_sha=main_sha,
                                 fail_envelope='{"class":"machinery"}')
    tid = db.create_thread(topic="w", session_id="s")
    db.update_thread(tid, main_repo_path=str(repo))
    t.set_topic_thread(db, "T-A", tid)

    # B depends on A and is blocked-failed (from A's earlier failure propagation).
    t.create_topic(db, topic_id="T-B", project_id="INBOX", title="T-B")
    g.create_task(db, task_id="T-B-k0", project_id="INBOX", title="k", prompt="p")
    g.set_task_topic(db, "T-B-k0", "T-B")
    g.replace_edges(db, "T-B-k0", ["T-A-k0"])  # cross-topic dep B → A
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET state='blocked-failed' WHERE id=? AND kind='topic'",
                     ("T-B",))
        conn.commit()

    result = tr.reconcile_topic_state(db, "T-A")

    assert result == "verified", "landed failed-integration topic must lift to verified"
    healed = t.get_topic(db, "T-A")
    assert healed["verified_at"] is not None
    assert (healed["fail_envelope"] or "") == "", "fail_envelope must be cleared"
    assert t.get_topic(db, "T-B")["state"] != "blocked-failed", "dependent must unblock"


def test_failed_integration_not_lifted_without_merge_proof(tmp_path):
    """A 'failed-integration' topic with NO merged_sha and nothing provable on
    main stays failed-integration (fail-closed — the verdict is preserved)."""
    db = _make_db(tmp_path)
    _mk_failed_integration_topic(db, "T-nofix", merged_sha=None,
                                 fail_envelope='{"class":"conflict"}')

    assert tr.reconcile_topic_state(db, "T-nofix") == "failed-integration"
    assert t.get_topic(db, "T-nofix")["fail_envelope"] is not None
