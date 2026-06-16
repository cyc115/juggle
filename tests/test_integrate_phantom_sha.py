"""Regression pins: integrate phantom merged_sha + stranded-work bug
(2026-06-16 incident).

ROOT CAUSES:
  A. _record_merged_sha persisted any SHA returned by git rev-parse with NO
     object-existence or ancestor-of-main check.  A worktree-local or phantom
     SHA could be written, making the topic stuck at 'integrating' forever
     (verified-gate correctly rejected it but work was already stranded).
  B. ahead_count==0 shortcut tore down worktree/branch based only on
     rev-list --count which can be stale (stale origin/main, local-only main).
     No merge-base --is-ancestor guard against canonical main.

FIX:
  A. _record_merged_sha MUST verify: object exists (cat-file -e) AND SHA is
     ancestor of origin/<main> (fetch first, fall back to local main).
  B. ahead_count==0 shortcut MUST run merge-base --is-ancestor before teardown.

All tests use isolated tmp git repos; no prod DB.
"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch, call

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from juggle_db import JuggleDB
from dbops import db_topics


# ── Shared helpers ────────────────────────────────────────────────────────────

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


def _make_repo(tmp_path, name="repo"):
    """Local git repo with one commit on 'main'; no remote."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)],
                   check=True, capture_output=True)
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    (repo / "base.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return str(repo)


def _make_repo_with_remote(tmp_path, name="local"):
    """Bare remote + local clone on 'main' with remote tracking."""
    remote = tmp_path / f"{name}.git"
    local = tmp_path / name
    local.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(local)],
                   check=True, capture_output=True)
    _git(local, "config", "user.email", "t@t.com")
    _git(local, "config", "user.name", "T")
    _git(local, "remote", "add", "origin", str(remote))
    (local / "base.py").write_text("x = 1\n")
    _git(local, "add", ".")
    _git(local, "commit", "-m", "base")
    _git(local, "push", "-u", "origin", "main")
    return str(local), str(remote)


def _make_worktree(repo_path, worktree_root, label):
    wt = str(Path(worktree_root) / f"wt-{label}")
    subprocess.run(
        ["git", "-C", repo_path, "worktree", "add", "-b", f"cyc_{label}", wt],
        check=True, capture_output=True,
    )
    _git(wt, "config", "user.email", "t@t.com")
    _git(wt, "config", "user.name", "T")
    return wt


def _add_commit(repo_path, filename, content, message):
    (Path(repo_path) / filename).write_text(content)
    subprocess.run(["git", "-C", repo_path, "add", filename],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", repo_path, "commit", "-m", message],
                   check=True, capture_output=True)


def _setup_db(tmp_path, prefix="t1"):
    d = JuggleDB(db_path=str(tmp_path / f"j_{prefix}.db"))
    d.init_db()
    tid = d.create_thread("test thread", session_id=f"sess_{prefix}")
    db_topics.create_topic(d, topic_id=f"tp_{prefix}", project_id="INBOX",
                           title="T1")
    db_topics.set_topic_thread(d, f"tp_{prefix}", tid)
    return d, tid, f"tp_{prefix}"


def _make_simple_db():
    db = Mock()
    db.update_thread = Mock()
    db.add_action_item = Mock()
    return db


# ── A. _record_merged_sha guards ─────────────────────────────────────────────

def test_record_merged_sha_non_ancestor_not_written(tmp_path):
    """2026-06-16: _record_merged_sha with a branch NOT merged to main must NOT
    write merged_sha. Current code omits ancestor check → this test is RED.
    Fix: add merge-base --is-ancestor guard before writing."""
    repo = _make_repo(tmp_path)
    db, tid, tp_id = _setup_db(tmp_path, "na")

    # Create unmerged side branch
    subprocess.run(["git", "-C", repo, "checkout", "-b", "cyc_side"],
                   check=True, capture_output=True)
    _add_commit(repo, "side.py", "s = 1\n", "side commit")
    subprocess.run(["git", "-C", repo, "checkout", "main"],
                   check=True, capture_output=True)
    # Confirm branch is NOT an ancestor of main
    r = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor", "cyc_side", "main"],
        capture_output=True,
    )
    assert r.returncode != 0, "test setup error: cyc_side should NOT be on main"

    from juggle_cmd_integrate import _record_merged_sha
    _record_merged_sha(db, tid, repo, "cyc_side")

    topic = db_topics.get_topic(db, tp_id)
    assert topic["merged_sha"] is None, (
        f"merged_sha must NOT be written for non-ancestor branch; got {topic['merged_sha']!r}"
    )


def test_record_merged_sha_nonexistent_ref_not_written(tmp_path):
    """_record_merged_sha with a ref that doesn't exist → merged_sha not written."""
    repo = _make_repo(tmp_path)
    db, tid, tp_id = _setup_db(tmp_path, "ne")

    from juggle_cmd_integrate import _record_merged_sha
    _record_merged_sha(db, tid, repo, "nonexistent-branch-xyz-99999")

    topic = db_topics.get_topic(db, tp_id)
    assert topic["merged_sha"] is None


def test_record_merged_sha_phantom_object_not_written(tmp_path):
    """2026-06-16: _record_merged_sha must refuse a SHA even if rev-parse would
    return it, when cat-file -e fails (object doesn't exist). Tests the
    cat-file -e guard via subprocess mocking."""
    repo = _make_repo(tmp_path)
    db, tid, tp_id = _setup_db(tmp_path, "ph")

    FAKE_SHA = "aabbccdd" * 5  # 40-char hex, doesn't exist in repo

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        # Intercept rev-parse to return our fake SHA
        if isinstance(cmd, list) and "rev-parse" in cmd and "cyc_phantom" in cmd:
            m = Mock()
            m.returncode = 0
            m.stdout = FAKE_SHA + "\n"
            return m
        return real_run(cmd, **kwargs)

    with patch("juggle_cmd_integrate.subprocess.run", side_effect=fake_run):
        from juggle_cmd_integrate import _record_merged_sha
        _record_merged_sha(db, tid, repo, "cyc_phantom")

    topic = db_topics.get_topic(db, tp_id)
    assert topic["merged_sha"] is None, (
        f"merged_sha must NOT be written for phantom/bad object; got {topic['merged_sha']!r}"
    )


def test_record_merged_sha_genuine_ancestor_written(tmp_path):
    """GREEN path: _record_merged_sha with a branch tip truly on main → SHA written."""
    repo = _make_repo(tmp_path)
    db, tid, tp_id = _setup_db(tmp_path, "ga")

    # Commit on worktree, then ff-merge to main → tip IS on main
    wt = _make_worktree(repo, str(tmp_path), "GA")
    _add_commit(wt, "feat.py", "y = 2\n", "feat: add y")
    # FF-merge worktree branch into main
    subprocess.run(["git", "-C", repo, "merge", "--ff-only", "cyc_GA"],
                   check=True, capture_output=True)
    # Now cyc_GA tip IS an ancestor of main (in fact == main)

    from juggle_cmd_integrate import _record_merged_sha
    _record_merged_sha(db, tid, repo, "cyc_GA")

    topic = db_topics.get_topic(db, tp_id)
    assert topic["merged_sha"] is not None, "merged_sha must be written for genuine ancestor"
    # SHA should equal cyc_GA tip
    expected = subprocess.run(
        ["git", "-C", repo, "rev-parse", "cyc_GA"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert topic["merged_sha"] == expected


# ── B. ahead_count==0 shortcut guard ─────────────────────────────────────────

def test_shortcut_non_ancestor_preserves_worktree_and_branch(tmp_path):
    """2026-06-16: ahead_count==0 shortcut with branch NOT truly an ancestor of
    canonical main → worktree + branch must NOT be torn down and fields NOT
    cleared. Current code omits merge-base --is-ancestor guard → RED.
    Fix: add guard before teardown."""
    from juggle_cmd_integrate import _run_integrate

    repo = _make_repo(tmp_path)
    wt = _make_worktree(repo, str(tmp_path), "PH")
    _add_commit(wt, "feat.py", "y = 1\n", "feat: unmerged work")

    db = _make_simple_db()
    thread = {
        "id": "t-ph",
        "worktree_path": wt,
        "worktree_branch": "cyc_PH",
        "main_repo_path": repo,
    }

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        # Force rev-list --count to return "0" (simulating stale/incorrect state)
        if (isinstance(cmd, list) and "rev-list" in cmd
                and "--count" in cmd and "cyc_PH" in str(cmd)):
            m = Mock()
            m.returncode = 0
            m.stdout = "0\n"
            return m
        return real_run(cmd, **kwargs)

    with patch("juggle_cmd_integrate.subprocess.run", side_effect=fake_run):
        with patch("juggle_cmd_integrate.get_repo_config",
                   return_value={"push_mode": "none", "test_cmd": ""}):
            with patch("juggle_integrate_lock._get_lock_path",
                       return_value=tmp_path / "t.lock"):
                ok, msg = _run_integrate(thread, db)

    # Work must be preserved: worktree still exists, branch still exists
    assert Path(wt).exists(), "worktree was incorrectly torn down despite branch not being merged"
    branches = subprocess.run(
        ["git", "-C", repo, "branch"], capture_output=True, text=True,
    ).stdout
    assert "cyc_PH" in branches, "branch was incorrectly deleted despite not being merged"

    # Thread fields must NOT be cleared (or integrate must return failure)
    # Either: ok=False (fail loudly), OR fields preserved (not cleared)
    if ok:
        # If it returned ok somehow, fields must NOT have been cleared
        db.update_thread.assert_not_called()
    else:
        # Correct: integrate detected the problem and returned failure
        pass


def test_shortcut_genuine_ancestor_cleans_up(tmp_path):
    """GREEN path: ahead_count==0 with branch genuinely on main → correct teardown."""
    from juggle_cmd_integrate import _run_integrate

    repo = _make_repo(tmp_path)
    wt = _make_worktree(repo, str(tmp_path), "GN")

    # Worktree branch has NO extra commits (== main tip) so it IS an ancestor
    # Leave it empty (0 commits ahead)

    db = _make_simple_db()
    thread = {
        "id": "t-gn",
        "worktree_path": wt,
        "worktree_branch": "cyc_GN",
        "main_repo_path": repo,
    }

    with patch("juggle_cmd_integrate.get_repo_config",
               return_value={"push_mode": "none", "test_cmd": ""}):
        with patch("juggle_integrate_lock._get_lock_path",
                   return_value=tmp_path / "t.lock"):
            with patch("juggle_cmd_integrate._restart_juggle_daemons"):
                ok, msg = _run_integrate(thread, db)

    assert ok, f"integrate should succeed for genuine already-merged branch: {msg}"
    assert not Path(wt).exists(), "worktree should be cleaned up after genuine merge"
    branches = subprocess.run(
        ["git", "-C", repo, "branch"], capture_output=True, text=True,
    ).stdout
    assert "cyc_GN" not in branches, "branch should be cleaned up"


def test_shortcut_genuine_ancestor_with_remote_cleans_up(tmp_path):
    """GREEN path with remote: branch pushed to origin, ahead_count==0 → correct teardown."""
    from juggle_cmd_integrate import _run_integrate

    local, remote = _make_repo_with_remote(tmp_path, "lcl")
    wt = _make_worktree(local, str(tmp_path), "GR")
    _add_commit(wt, "feat.py", "y = 2\n", "feat: add y")

    # Push the feature branch to origin and ff-merge → local + remote both at same tip
    subprocess.run(["git", "-C", local, "merge", "--ff-only", "cyc_GR"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", local, "push", "origin", "main"],
                   check=True, capture_output=True)
    # Now origin/main == local main == cyc_GR tip → ahead_count = 0

    db = _make_simple_db()
    thread = {
        "id": "t-gr",
        "worktree_path": wt,
        "worktree_branch": "cyc_GR",
        "main_repo_path": local,
    }

    with patch("juggle_cmd_integrate.get_repo_config",
               return_value={"push_mode": "none", "test_cmd": ""}):
        with patch("juggle_integrate_lock._get_lock_path",
                   return_value=tmp_path / "t.lock"):
            with patch("juggle_cmd_integrate._restart_juggle_daemons"):
                ok, msg = _run_integrate(thread, db)

    assert ok, f"should clean up when branch is genuinely merged to origin/main: {msg}"
    assert not Path(wt).exists()
