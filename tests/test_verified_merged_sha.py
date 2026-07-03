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
from unittest.mock import patch

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


@pytest.fixture
def git_repo_with_remote(tmp_path):
    """Bare remote + local clone on branch 'main', remote tracking set up."""
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    local.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(local)], check=True, capture_output=True)
    for cmd in [
        ["git", "-C", str(local), "config", "user.email", "t@t.com"],
        ["git", "-C", str(local), "config", "user.name", "T"],
        ["git", "-C", str(local), "remote", "add", "origin", str(remote)],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    (local / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(local), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "branch", "-M", "main"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(local), "push", "-u", "origin", "main"],
        check=True, capture_output=True,
    )
    return str(local), str(remote)


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
    for i in range(2):
        nid = f"{topic_id}-k{i}"
        g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt="p")
        g.set_task_topic(db, nid, topic_id)  # dual-writes nodes.parent_id
    # Topic state + member states read from nodes (P8 Task 4.2; legacy dropped).
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET state='integrating' WHERE id=? AND kind='topic'",
            (topic_id,),
        )
        for i in range(2):
            nid = f"{topic_id}-k{i}"
            conn.execute("UPDATE nodes SET state='verified' WHERE id=?", (nid,))
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


# ---------------------------------------------------------------------------
# defect C (2026-07-01): reconcile self-heals integrating-but-merged topics
#
# INCIDENT: T-fix-max-agents-config stuck at 'integrating' post-merge. integrate
# recorded merged_sha BEFORE the push (checked ancestry against origin/main which
# did not yet contain the commit) → merged_sha NULL (swallowed). mark_topic_
# completion raised UnmergedVerifyRefused on →verified which mark_graph_topic
# swallowed → topic stuck 'integrating'; reconcile re-derived 'integrating'
# through the same NULL-sha gate and could NOT repair.
# ---------------------------------------------------------------------------


def _ffmerge_branch_into_main(repo: Path, branch: str) -> str:
    """Create ``branch``, add a commit, ff-merge it into main. Returns its sha
    (now an ancestor of main, but NOT recorded anywhere on the topic)."""
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / "f.txt").write_text("feat\n")
    _git(repo, "commit", "-aqm", "feat")
    sha = _git(repo, "rev-parse", branch).stdout.strip()
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", branch)
    return sha


def test_reconcile_heals_integrating_when_branch_merged_but_sha_null(db, tmp_path):
    """RED pin (2026-07-01 T-fix-max-agents-config): topic at 'integrating', all
    tasks verified, branch provably merged to main, merged_sha NULL. reconcile
    must re-derive merged_sha from git reality and complete →verified."""
    repo = _make_repo(tmp_path)
    sha = _ffmerge_branch_into_main(repo, "cyc_x")  # _mk_topic binds worktree_branch='cyc_x'
    _mk_topic_all_verified(db, "T-heal", repo)      # integrating + sha NULL
    assert t.get_topic(db, "T-heal")["merged_sha"] is None

    result = tr.reconcile_topic_state(db, "T-heal")

    assert result == "verified"
    healed = t.get_topic(db, "T-heal")
    assert healed["merged_sha"] == sha, "reconcile must RECORD the merged_sha it derived"
    assert healed["verified_at"] is not None


def test_reconcile_does_not_heal_when_branch_not_ancestor(db, tmp_path):
    """Self-heal must NEVER verify without a real merged sha: an unmerged branch
    tip (not an ancestor of main) leaves the topic at 'integrating', sha NULL."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "cyc_x")      # branch exists but NOT merged
    (repo / "f.txt").write_text("wip\n")
    _git(repo, "commit", "-aqm", "wip")
    _git(repo, "checkout", "-q", "main")
    _mk_topic_all_verified(db, "T-nom", repo)

    assert tr.reconcile_topic_state(db, "T-nom") == "integrating"
    assert t.get_topic(db, "T-nom")["merged_sha"] is None


def test_mark_graph_topic_self_heals_instead_of_silently_sticking(db, tmp_path):
    """RED pin (2026-07-01): mark_graph_topic swallowed UnmergedVerifyRefused and
    left the topic stuck at 'integrating'. It must NOT swallow silently — self-heal
    via reconcile (which records the merged_sha) so the topic reaches 'verified'."""
    from juggle_cmd_agents_graph_topics import mark_graph_topic

    repo = _make_repo(tmp_path)
    _ffmerge_branch_into_main(repo, "cyc_x")
    _mk_topic_all_verified(db, "T-mk", repo)         # integrating + sha NULL
    tid = t.get_topic(db, "T-mk")["thread_id"]

    mark_graph_topic(db, tid, integrate_ok=True, handoff="h", session_id="s")

    assert t.get_topic(db, "T-mk")["state"] == "verified"


def test_worktree_branch_recorded_at_dispatch(db):
    t.create_topic(db, topic_id="a", project_id="INBOX", title="Topic a")
    nid = "a-k0"
    g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt="p")
    g.set_task_topic(db, nid, "a")  # dual-writes nodes.parent_id (P8 Task 4.2)
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


# ---------------------------------------------------------------------------
# defect (2026-07-02, async-land): stuck-integrating topic with NO merged_sha
# recorded, even though a watchdog-owned integrate demonstrably COMPLETED
# (branch merged + deleted, thread worktree fields cleared).
#
# RCA: `_record_merged_sha` silently skips recording when its ancestry check
# fails/is inconclusive (fail-soft by design). The very next steps of the SAME
# `_run_integrate` call delete the git branch (`remove_workspace`) and clear
# `thread.worktree_branch`/`main_repo_path` (finalize) — so the pre-existing
# 2026-07-01 self-heal (`_heal_merged_sha`, branch-name based) runs immediately
# afterward (via `mark_graph_topic`'s UnmergedVerifyRefused handler) with
# NOTHING left to re-derive from: no branch to resolve, no repo on the thread.
# The topic wedges at 'integrating' forever; no later reconcile/orphan sweep
# can repair it either (same dead branch, same cleared thread).
#
# FIX: `_record_merged_sha` stashes the resolved-but-unproven SHA (+ repo) on
# `nodes.pending_merged_sha`/`pending_merged_repo` — durable, topic-scoped,
# independent of the thread/branch. `_heal_merged_sha` re-checks the pending
# SHA's ancestry (against CURRENT canonical main) and promotes it once proven.
# ---------------------------------------------------------------------------


def test_record_merged_sha_stashes_pending_when_canonical_unresolvable(db, tmp_path):
    """RED pin: a resolved, real, genuinely-merged SHA whose ancestry check
    can't be completed (canonical main unresolvable) must not be dropped —
    it must survive as pending_merged_sha/pending_merged_repo for self-heal."""
    from juggle_cmd_integrate import _record_merged_sha

    repo = _make_repo(tmp_path)
    sha = _ffmerge_branch_into_main(repo, "cyc_pend")
    tid = db.create_thread(topic="w", session_id="s")
    t.create_topic(db, topic_id="T-pend", project_id="INBOX", title="F")
    t.set_topic_thread(db, "T-pend", tid)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("juggle_integrate_mergedsha._canonical_main_ref", lambda repo: None)
        _record_merged_sha(db, tid, str(repo), sha)

    topic = t.get_topic(db, "T-pend")
    assert topic["merged_sha"] is None, "must not fabricate a merged_sha without proof"
    assert topic["pending_merged_sha"] == sha
    assert topic["pending_merged_repo"] == str(repo)


def test_heal_merged_sha_promotes_pending_when_branch_already_gone(db, tmp_path):
    """RED pin: reconcile self-heal must recover via pending_merged_sha even
    when the thread's worktree fields are already cleared and the git branch
    itself is already deleted (the post-`_run_integrate`-teardown state) —
    the pre-2026-07-02 branch-name-only self-heal has nothing to work with
    here and returns 'integrating' forever."""
    repo = _make_repo(tmp_path)
    sha = _ffmerge_branch_into_main(repo, "cyc_gone")
    _git(repo, "branch", "-D", "cyc_gone")  # teardown already deleted the branch

    _mk_topic_all_verified(db, "T-gone", repo=None, bind=False)  # thread_id NULL
    t.set_topic_pending_merged_sha(db, "T-gone", sha, repo=str(repo))

    result = tr.reconcile_topic_state(db, "T-gone")

    assert result == "verified"
    healed = t.get_topic(db, "T-gone")
    assert healed["merged_sha"] == sha
    assert healed["pending_merged_sha"] is None
    assert healed["verified_at"] is not None


def test_heal_merged_sha_does_not_promote_pending_not_on_main(db, tmp_path):
    """Self-heal must never promote a pending SHA that is NOT actually an
    ancestor of main — the ancestry re-check must be real, not a bypass."""
    repo = _make_repo(tmp_path)
    sha = _unmerged_sha(repo)  # side branch, never merged

    _mk_topic_all_verified(db, "T-badpend", repo=None, bind=False)
    t.set_topic_pending_merged_sha(db, "T-badpend", sha, repo=str(repo))

    assert tr.reconcile_topic_state(db, "T-badpend") == "integrating"
    assert t.get_topic(db, "T-badpend")["merged_sha"] is None


def test_async_land_watchdog_owned_integrate_self_heals_end_to_end(git_repo_with_remote, tmp_path):
    """RED pin (2026-07-02 async-land incident): drive a REAL watchdog-owned
    completion (`_run_integrate` then `mark_graph_topic`, mirroring
    `cmd_complete_agent`) through a transient ancestry-check failure at record
    time. Pre-fix: topic wedges at 'integrating' with merged_sha NULL forever
    (branch deleted + thread cleared by the time self-heal runs). Post-fix:
    the topic still reaches 'verified' with a real merged_sha in the SAME
    completion call, because the pending breadcrumb survives the teardown."""
    from juggle_cmd_integrate import _run_integrate
    from juggle_cmd_agents_graph_topics import mark_graph_topic
    from juggle_db import JuggleDB
    from dbops import db_topics
    from dbops import db_graph as g

    local, remote = git_repo_with_remote
    wt = str(Path(tmp_path) / "wt-AL")
    subprocess.run(
        ["git", "-C", local, "worktree", "add", "-b", "cyc_AL", wt],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "-C", wt, "config", "user.email", "t@t.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", wt, "config", "user.name", "T"], check=True, capture_output=True)
    (Path(wt) / "feat.py").write_text("y = 2\n")
    subprocess.run(["git", "-C", wt, "add", "feat.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", wt, "commit", "-m", "feat: async-land"], check=True, capture_output=True)

    db = JuggleDB(db_path=str(tmp_path / "j_al.db"))
    db.init_db()
    tid = db.create_thread("t", session_id="s")
    db.update_thread(tid, worktree_path=wt, worktree_branch="cyc_AL", main_repo_path=local)
    db_topics.create_topic(db, topic_id="T-async-land", project_id="INBOX", title="F")
    db_topics.set_topic_thread(db, "T-async-land", tid)
    g.create_task(db, task_id="T-async-land-k0", project_id="INBOX", title="k0", prompt="p")
    g.set_task_topic(db, "T-async-land-k0", "T-async-land")
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET state='verified' WHERE id=?", ("T-async-land-k0",))
        conn.commit()
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start"):
        db_topics.topic_transition(db, "T-async-land", ev)

    thread = db.get_thread(tid)
    with pytest.MonkeyPatch.context() as mp:
        # Simulate the transient race: the ancestry check at RECORD time can't
        # resolve canonical main (e.g. a stale/unfetched origin/<main> tracking
        # ref) even though the ff-merge + push just landed the commit for real.
        mp.setattr("juggle_integrate_mergedsha._canonical_main_ref", lambda repo: None)
        with patch("juggle_cmd_integrate.get_repo_config",
                   return_value={"push_mode": "direct", "test_cmd": ""}):
            with patch("juggle_integrate_lock._get_lock_path", return_value=tmp_path / "t.lock"):
                with patch("juggle_cmd_integrate._restart_juggle_daemons"):
                    ok, msg = _run_integrate(thread, db)
    assert ok, msg

    # Integrate itself could not record merged_sha (canonical unresolvable).
    assert db_topics.get_topic(db, "T-async-land")["merged_sha"] is None
    # ...but the pending breadcrumb survived the branch delete + field clear.
    pending = db_topics.get_topic(db, "T-async-land")
    assert pending["pending_merged_sha"], "pending_merged_sha must survive teardown"

    mark_graph_topic(db, tid, True, "async-land done", "s")

    healed = db_topics.get_topic(db, "T-async-land")
    assert healed["state"] == "verified", (
        f"topic must self-heal to verified in the same completion call, got {healed['state']!r}"
    )
    assert healed["merged_sha"], "merged_sha must be recorded via the self-heal"
    r = subprocess.run(
        ["git", "-C", remote, "merge-base", "--is-ancestor", healed["merged_sha"], "main"],
        capture_output=True,
    )
    assert r.returncode == 0, "recorded merged_sha must be a real ancestor of the pushed main"
