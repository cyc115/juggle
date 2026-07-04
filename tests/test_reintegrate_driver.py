"""Fix 1 — watchdog re-integrate driver (the core of the integrate-wedge fix).

Incident (2026-07-03 integrate-wedge RCA): three topics (T-rail-color-palette,
T-cockpit-done-header, T-gp-migration) sat in state='integrating' for 1–1.5 h
with real committed-but-unmerged work and no integrate running. The only
merge-lander (complete-agent → _run_integrate) runs ONCE, inline; a single miss
= permanent wedge because the watchdog had NO re-integrate driver. graph_tick
re-dispatches only 'ready' topics; the repair sweep needs a fail_envelope (none
was written); orphan-reconcile skips a topic bound to a busy agent.

This pins the level-triggered re-integrate sweep (k8s reconcile + Restart=
on-failure): observed git state is the oracle.
  * LANDED (incl. rebased, via the two-tier oracle) → heal merged_sha, advance
    to 'verified', NEVER re-merge.
  * non-landed + real commits ahead + no live bound agent → idempotently re-run
    integrate; a real failure → fail_envelope + 'failed-integration' (repair
    sweep owns it thereafter).
  * a topic with a live busy bound agent is never touched (it may be
    mid-finalize).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _no_backoff():
    """Drive the sweep with zero grace/backoff so a single call re-integrates."""
    import juggle_graph_reintegrate as ri
    ri.reset_backoff()
    with patch.object(ri, "REINTEGRATE_GRACE_SECS", 0), \
         patch.object(ri, "REINTEGRATE_BACKOFF_SECS", 0):
        yield
    ri.reset_backoff()


def _make_db(tmp_path):
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return str(repo)


def _worktree(repo, root, label):
    wt = str(Path(root) / f"wt-{label}")
    _git(repo, "worktree", "add", "-b", f"cyc_{label}", wt)
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    return wt


def test_reintegrate_lands_wedged_topic(tmp_path):
    """The incident case: integrating topic, real unmerged commit, no live agent
    → the driver re-runs integrate, the work merges, topic → verified."""
    import juggle_graph_reintegrate as ri
    from dbops.db_topics import get_topic

    repo = _repo(tmp_path)
    wt = _worktree(repo, tmp_path, "AB")
    (Path(wt) / "feat.py").write_text("y = 2\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "feat: work")

    db = _make_db(tmp_path)
    tid = db.create_thread(topic="feat", session_id="s")
    db.update_thread(tid, worktree_path=wt, worktree_branch="cyc_AB",
                     main_repo_path=repo)
    _seed_topic_with_thread(db, "T1", tid, repo, "cyc_AB")

    with patch("juggle_cmd_integrate.get_repo_config",
               return_value={"push_mode": "none", "test_cmd": ""}), \
         patch("juggle_integrate_lock._get_lock_path",
               return_value=tmp_path / "t.lock"), \
         patch("juggle_cmd_integrate._restart_juggle_daemons"):
        driven = ri.sweep_reintegrate(db, ["INBOX"])

    assert "T1" in driven
    assert get_topic(db, "T1")["state"] == "verified"
    assert (get_topic(db, "T1")["merged_sha"] or "").strip()
    assert (Path(repo) / "feat.py").exists()   # merged to main
    assert not Path(wt).exists()               # worktree cleaned up


def test_reintegrate_never_re_merges_rebased_landing(tmp_path):
    """Amendment blind spot: a topic whose branch already landed via REBASE
    (equivalent commit on main under a different sha, branch tip NOT an ancestor)
    must heal to verified WITHOUT calling _run_integrate — re-merging would
    duplicate the commit / raise a spurious conflict."""
    import juggle_graph_reintegrate as ri
    from dbops.db_topics import get_topic

    repo = _repo(tmp_path)
    # branch cyc_RB with a real commit, then rebase-land its patch on main.
    _git(repo, "checkout", "-b", "cyc_RB")
    (Path(repo) / "feat.py").write_text("y = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat")
    _git(repo, "checkout", "main")
    (Path(repo) / "mainside.py").write_text("m = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "mainside")
    _git(repo, "cherry-pick", "cyc_RB")

    db = _make_db(tmp_path)
    tid = db.create_thread(topic="feat", session_id="s")
    db.update_thread(tid, worktree_path="", worktree_branch="cyc_RB",
                     main_repo_path=repo)
    _seed_topic_with_thread(db, "T1", tid, repo, "cyc_RB")

    with patch("juggle_graph_reintegrate._run_integrate") as spy:
        driven = ri.sweep_reintegrate(db, ["INBOX"])

    spy.assert_not_called()   # landed → NEVER re-merge
    assert "T1" in driven
    assert get_topic(db, "T1")["state"] == "verified"


def test_reintegrate_skips_topic_with_live_busy_agent(tmp_path):
    """A topic whose dispatch thread still has a live busy agent may be
    mid-finalize — the driver must not re-run integrate under it."""
    import juggle_graph_reintegrate as ri
    from dbops.db_topics import get_topic

    repo = _repo(tmp_path)
    wt = _worktree(repo, tmp_path, "LV")
    (Path(wt) / "feat.py").write_text("y = 2\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "feat")

    db = _make_db(tmp_path)
    tid = db.create_thread(topic="feat", session_id="s")
    db.update_thread(tid, worktree_path=wt, worktree_branch="cyc_LV",
                     main_repo_path=repo)
    _seed_topic_with_thread(db, "T1", tid, repo, "cyc_LV")
    agent_id = db.create_agent(role="coder", pane_id="p1")
    assert db.cas_assign_agent(agent_id, tid)   # busy on the topic's thread

    with patch("juggle_graph_reintegrate._run_integrate") as spy:
        driven = ri.sweep_reintegrate(db, ["INBOX"])

    spy.assert_not_called()
    assert "T1" not in driven
    assert get_topic(db, "T1")["state"] == "integrating"   # left untouched


def test_reintegrate_routes_real_failure_to_failed_integration(tmp_path):
    """A genuine integrate failure (rebase conflict) → topic 'failed-integration'
    with a fail_envelope, so the existing repair sweep picks it up. Never left
    silently wedged in 'integrating'."""
    import juggle_graph_reintegrate as ri
    from dbops.db_topics import get_topic

    repo = _repo(tmp_path)
    # main advances a.py; the branch edits the SAME line → rebase conflict.
    wt = _worktree(repo, tmp_path, "CF")
    (Path(wt) / "a.py").write_text("x = 999\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "branch edit")
    (Path(repo) / "a.py").write_text("x = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "main edit")

    db = _make_db(tmp_path)
    tid = db.create_thread(topic="feat", session_id="s")
    db.update_thread(tid, worktree_path=wt, worktree_branch="cyc_CF",
                     main_repo_path=repo)
    _seed_topic_with_thread(db, "T1", tid, repo, "cyc_CF")

    with patch("juggle_cmd_integrate.get_repo_config",
               return_value={"push_mode": "none", "test_cmd": ""}), \
         patch("juggle_integrate_lock._get_lock_path",
               return_value=tmp_path / "t.lock"), \
         patch("juggle_cmd_integrate._restart_juggle_daemons"):
        ri.sweep_reintegrate(db, ["INBOX"])

    topic = get_topic(db, "T1")
    assert topic["state"] == "failed-integration"
    assert topic["fail_envelope"], "a fail_envelope must route it to the repair sweep"


def test_run_tick_sweeps_invokes_reintegrate_driver(tmp_path, monkeypatch):
    """Wiring pin: the watchdog tick (juggle_graph_repair.run_tick_sweeps, called
    every cycle by the daemon) drives the re-integrate sweep."""
    import juggle_graph_reintegrate as ri
    import juggle_graph_repair as repair

    db = _make_db(tmp_path)
    called = {}
    monkeypatch.setattr(ri, "run_reintegrate_tick",
                        lambda _db: called.setdefault("hit", True))

    repair.run_tick_sweeps(db)

    assert called.get("hit") is True


# ── shared seeder (kept below the tests that use it for readability) ──────────

def _seed_topic_with_thread(db, topic_id, thread_id, repo, branch):
    from dbops import db_topics, db_graph
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    db_topics.create_topic(db, topic_id=topic_id, project_id="INBOX",
                           title=f"Topic {topic_id}")
    with db._connect() as c:
        c.execute("UPDATE nodes SET state='integrating', worktree_branch=?, "
                  "main_repo_path=?, updated_at=? WHERE id=? AND kind='topic'",
                  (branch, repo, now, topic_id))
        c.commit()
    db_topics.set_topic_thread(db, topic_id, thread_id)
    task = f"{topic_id}-t0"
    db_graph.create_task(db, task_id=task, project_id="INBOX", title=task, prompt="x")
    db_graph.set_task_topic(db, task, topic_id)
    with db._connect() as c:
        c.execute("UPDATE nodes SET state='verified' WHERE id=?", (task,))
        c.commit()
