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
  * non-landed + real commits ahead + no live bound agent → spawn a DETACHED
    integrate subprocess (never run the ~7-min full-suite gate inline on the
    watchdog tick — that trips the tickguard budget and livelocks the daemon);
    the subprocess's outcome reconciles on a LATER tick (landed → verified;
    failed → fail_envelope → 'failed-integration', repair sweep owns it).
  * a topic with a live busy bound agent is never touched (it may be
    mid-finalize).

T-fix-reintegrate-offtick regression pins (2026-07-03): the tick must SELECT +
SPAWN only, never block on the gate; a subprocess killed mid-gate (tickguard
daemon restart) is retried on a later tick and eventually lands.
"""
from __future__ import annotations

import subprocess
import sys
import time
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


class _AliveProc:
    """Stand-in for a detached integrate subprocess still running the gate."""
    def poll(self):
        return None


class _DoneProc:
    """Stand-in for a detached integrate subprocess that exited cleanly."""
    def poll(self):
        return 0


class _KilledProc:
    """Stand-in for a subprocess the tickguard daemon restart killed mid-gate."""
    def poll(self):
        return -9


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


def _integrate_patches(tmp_path):
    """The three side-effect stubs a real _run_integrate needs in a test repo."""
    return (
        patch("juggle_cmd_integrate.get_repo_config",
              return_value={"push_mode": "none", "test_cmd": ""}),
        patch("juggle_integrate_lock._get_lock_path",
              return_value=tmp_path / "t.lock"),
        patch("juggle_cmd_integrate._restart_juggle_daemons"),
    )


def _spawn_runs_integrate_inline(tmp_path):
    """Fake _spawn_detached_integrate: run the gate synchronously (simulating the
    detached subprocess running to completion) and return a finished proc.

    Mirrors what the real detached CLI `integrate` does — merge the git work but
    leave the topic in 'integrating' for the driver's next-tick reconcile."""
    def _spawn(thread, db):
        from juggle_cmd_integrate import _run_integrate
        p1, p2, p3 = _integrate_patches(tmp_path)
        with p1, p2, p3:
            _run_integrate(thread, db)
        return _DoneProc()
    return _spawn


def test_reintegrate_spawns_then_lands_on_later_tick(tmp_path):
    """The incident case, now OFF the tick: sweep 1 SPAWNS a detached integrate
    (topic still 'integrating'); the merge lands; a LATER sweep reconciles the
    landed git state → 'verified'. The gate never runs inline on the tick."""
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

    with patch("juggle_graph_reintegrate._spawn_detached_integrate",
               side_effect=_spawn_runs_integrate_inline(tmp_path)):
        driven1 = ri.sweep_reintegrate(db, ["INBOX"])
        # Tick 1: spawned only — reconcile-to-verified is a LATER tick's job.
        assert "T1" in driven1
        assert (Path(repo) / "feat.py").exists()   # subprocess merged to main
        assert not Path(wt).exists()               # worktree cleaned up

        driven2 = ri.sweep_reintegrate(db, ["INBOX"])

    assert "T1" in driven2
    assert get_topic(db, "T1")["state"] == "verified"
    assert (get_topic(db, "T1")["merged_sha"] or "").strip()


def test_reintegrate_never_re_merges_rebased_landing(tmp_path):
    """Amendment blind spot: a topic whose branch already landed via REBASE
    (equivalent commit on main under a different sha, branch tip NOT an ancestor)
    must heal to verified WITHOUT spawning integrate — re-merging would duplicate
    the commit / raise a spurious conflict."""
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

    with patch("juggle_graph_reintegrate._spawn_detached_integrate") as spy:
        driven = ri.sweep_reintegrate(db, ["INBOX"])

    spy.assert_not_called()   # landed → NEVER re-merge
    assert "T1" in driven
    assert get_topic(db, "T1")["state"] == "verified"


def test_reintegrate_skips_topic_with_live_busy_agent(tmp_path):
    """A topic whose dispatch thread still has a live busy agent may be
    mid-finalize — the driver must not spawn integrate under it."""
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

    with patch("juggle_graph_reintegrate._spawn_detached_integrate") as spy:
        driven = ri.sweep_reintegrate(db, ["INBOX"])

    spy.assert_not_called()
    assert "T1" not in driven
    assert get_topic(db, "T1")["state"] == "integrating"   # left untouched


def test_reintegrate_tick_runs_gate_off_thread_within_budget(tmp_path):
    """Regression pin (T-fix-reintegrate-offtick): a candidate needing a full
    integrate must NOT run the gate inline on the tick — the tick only selects +
    spawns and returns immediately, well within the 90s tickguard budget, even
    if the integrate gate itself would take minutes."""
    import juggle_graph_reintegrate as ri

    repo = _repo(tmp_path)
    wt = _worktree(repo, tmp_path, "SL")
    (Path(wt) / "feat.py").write_text("y = 2\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "feat")

    db = _make_db(tmp_path)
    tid = db.create_thread(topic="feat", session_id="s")
    db.update_thread(tid, worktree_path=wt, worktree_branch="cyc_SL",
                     main_repo_path=repo)
    _seed_topic_with_thread(db, "T1", tid, repo, "cyc_SL")

    def _slow_gate(*a, **k):
        time.sleep(30)   # would blow the tickguard budget if run inline
        return True, "ok"

    with patch("juggle_cmd_integrate._run_integrate",
               side_effect=_slow_gate) as gate, \
         patch("juggle_graph_reintegrate._spawn_detached_integrate",
               return_value=_AliveProc()) as spawn:
        t0 = time.monotonic()
        driven = ri.sweep_reintegrate(db, ["INBOX"])
        elapsed = time.monotonic() - t0

    assert "T1" in driven          # candidate selected + spawned
    spawn.assert_called_once()     # gate handed OFF to a detached subprocess
    assert gate.call_count == 0    # gate NEVER run inline on the tick
    assert elapsed < 5.0           # nowhere near the 90s tickguard budget


def test_reintegrate_skips_respawn_while_prior_integrate_in_flight(tmp_path):
    """Single-flight: while a spawned detached integrate is still running the
    gate, a later tick must NOT spawn a second one (nor count it / fail it) —
    its outcome reconciles on a still-later tick."""
    import juggle_graph_reintegrate as ri

    repo = _repo(tmp_path)
    wt = _worktree(repo, tmp_path, "IF")
    (Path(wt) / "feat.py").write_text("y = 2\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "feat")

    db = _make_db(tmp_path)
    tid = db.create_thread(topic="feat", session_id="s")
    db.update_thread(tid, worktree_path=wt, worktree_branch="cyc_IF",
                     main_repo_path=repo)
    _seed_topic_with_thread(db, "T1", tid, repo, "cyc_IF")

    with patch("juggle_graph_reintegrate._spawn_detached_integrate",
               return_value=_AliveProc()) as spawn:
        ri.sweep_reintegrate(db, ["INBOX"])   # tick 1: spawns
        ri.sweep_reintegrate(db, ["INBOX"])   # tick 2: in flight → no re-spawn
        ri.sweep_reintegrate(db, ["INBOX"])   # tick 3: still in flight → no re-spawn

    spawn.assert_called_once()


def test_reintegrate_killed_mid_gate_is_retried_and_lands(tmp_path):
    """Regression pin (T-fix-reintegrate-offtick): a detached integrate the
    tickguard daemon restart killed mid-gate (nothing merged, proc dead) is
    re-spawned on a later tick and eventually lands → 'verified'."""
    import juggle_graph_reintegrate as ri
    from dbops.db_topics import get_topic

    repo = _repo(tmp_path)
    wt = _worktree(repo, tmp_path, "KM")
    (Path(wt) / "feat.py").write_text("y = 2\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "feat")

    db = _make_db(tmp_path)
    tid = db.create_thread(topic="feat", session_id="s")
    db.update_thread(tid, worktree_path=wt, worktree_branch="cyc_KM",
                     main_repo_path=repo)
    _seed_topic_with_thread(db, "T1", tid, repo, "cyc_KM")

    calls = {"n": 0}

    def _spawn(thread, db):
        calls["n"] += 1
        if calls["n"] == 1:
            return _KilledProc()   # killed mid-gate — nothing merged
        from juggle_cmd_integrate import _run_integrate
        p1, p2, p3 = _integrate_patches(tmp_path)
        with p1, p2, p3:
            _run_integrate(thread, db)   # retry completes the merge
        return _DoneProc()

    with patch("juggle_graph_reintegrate._spawn_detached_integrate",
               side_effect=_spawn):
        ri.sweep_reintegrate(db, ["INBOX"])   # tick 1: spawn#1 killed
        assert get_topic(db, "T1")["state"] == "integrating"
        ri.sweep_reintegrate(db, ["INBOX"])   # tick 2: dead → respawn#2 merges
        assert get_topic(db, "T1")["state"] == "integrating"
        ri.sweep_reintegrate(db, ["INBOX"])   # tick 3: reconcile → verified

    assert calls["n"] == 2   # retried exactly once after the kill
    assert get_topic(db, "T1")["state"] == "verified"


def test_spawn_detached_integrate_command_and_env(tmp_path):
    """Pin the integration seam: the detached spawn invokes the `integrate` CLI
    subcommand for the thread, detached (start_new_session), on the SAME db
    (JUGGLE_DB_PATH) and marked watchdog-owned (JUGGLE_ORCHESTRATOR) so the
    watchdog-owned-integrate guard permits it. A rename of the CLI subcommand or
    a dropped env var breaks the whole off-tick fix silently — this catches it."""
    import subprocess as real_subprocess

    import juggle_graph_reintegrate as ri

    class _FakeDB:
        db_path = str(tmp_path / "j.db")

    thread = {"id": "T-uuid-123", "main_repo_path": str(tmp_path)}
    captured = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _DoneProc()

    with patch.object(real_subprocess, "Popen", side_effect=_fake_popen):
        proc = ri._spawn_detached_integrate(thread, _FakeDB())

    assert proc is not None
    argv = captured["argv"]
    assert argv[-2:] == ["integrate", "T-uuid-123"]   # `juggle integrate <thread>`
    assert argv[1].endswith("juggle_cli.py")
    kw = captured["kwargs"]
    assert kw["start_new_session"] is True             # survives daemon restart
    assert kw["cwd"] == str(tmp_path)
    assert kw["env"]["JUGGLE_ORCHESTRATOR"] == "1"     # watchdog-owned → guard permits
    assert kw["env"]["JUGGLE_DB_PATH"] == str(tmp_path / "j.db")  # same db


def test_spawn_detached_integrate_refuses_without_repo(tmp_path):
    """No main_repo_path → nothing to integrate; never spawn a bogus process."""
    import juggle_graph_reintegrate as ri

    assert ri._spawn_detached_integrate({"id": "T", "main_repo_path": ""}, object()) is None
    assert ri._spawn_detached_integrate({"id": "", "main_repo_path": "/x"}, object()) is None


def test_reintegrate_routes_failed_subprocess_to_failed_integration(tmp_path):
    """A detached integrate that FAILED (rebase conflict → fail_envelope written,
    but subprocess exited without landing) is routed to 'failed-integration' on a
    later tick, so the existing repair sweep picks it up."""
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

    with patch("juggle_graph_reintegrate._spawn_detached_integrate",
               side_effect=_spawn_runs_integrate_inline(tmp_path)):
        ri.sweep_reintegrate(db, ["INBOX"])   # tick 1: spawn → conflict → fail_envelope
        assert get_topic(db, "T1")["state"] == "integrating"   # not yet routed
        ri.sweep_reintegrate(db, ["INBOX"])   # tick 2: fail_envelope → route

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
