"""RC1 regression pins — complete-time integrate must spawn DETACHED, never inline.

Incident (2026-07-04 inline-gate death by watchdog respawn / integrate-wedge #2):
complete-agent ran the merge gate INLINE (cmd_complete_agent → _run_integrate)
inside the spool-apply, which executes in the WATCHDOG process. The watchdog
self-restarts whenever plugin HEAD advances, and every successful integrate
advances HEAD — so restarts are frequent and killed any in-flight inline gate.
T-gp-{cancel,retry,edit} sat wedged 'integrating' with bound-busy agents 45+ min.

Fix: complete-agent marks the bound TOPIC 'integrating' and spawns the SAME
detached integrate the re-integrate sweep uses (start_new_session, watchdog-owned
env), then returns WITHOUT waiting for the gate. Nothing merge-landing may ever
run inline in the watchdog/spool process; the reconcile tick lands it later.
"""
from __future__ import annotations

import argparse
import subprocess as real_subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB


class _AliveProc:
    """A detached integrate still running its gate — proves complete-agent
    returned WITHOUT waiting for the gate to finish."""
    def poll(self):
        return None


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    import juggle_cli_common as common
    import juggle_cmd_agents_common

    monkeypatch.setattr(common, "get_db", lambda: d)
    monkeypatch.setattr(juggle_cmd_agents_common, "get_db", lambda: d)
    # Orchestrator context: cmd_complete_agent runs directly (never spools).
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    return d


def _args(thread_id, summary="done"):
    return argparse.Namespace(
        thread_id=thread_id, result_summary=summary, retain_text=None,
        open_questions=None, handoff="handed off", role="coder",
    )


def _seed_running_topic(db, thread_id, topic_id, repo):
    """A topic in 'running' with ALL member tasks 'verified', bound to a worktree
    thread — the integrate-worthy shape complete-agent sees for an autopilot
    topic coder that just finished (the incident: T-gp-{cancel,retry,edit})."""
    from dbops import db_graph, db_topics

    db_topics.create_topic(db, topic_id=topic_id, project_id="INBOX",
                           title=f"Topic {topic_id}")
    task = f"{topic_id}-t0"
    db_graph.create_task(db, task_id=task, project_id="INBOX", title=task, prompt="x")
    db_graph.set_task_topic(db, task, topic_id)
    with db._connect() as c:
        c.execute("UPDATE nodes SET state='running' WHERE id=? AND kind='topic'",
                  (topic_id,))
        c.execute("UPDATE nodes SET state='verified' WHERE id=?", (task,))
        c.commit()
    db_topics.set_topic_thread(db, topic_id, thread_id)


def test_complete_agent_spawns_detached_integrate_not_inline(db):
    """RC1 pin: a topic-bound completion spawns a DETACHED integrate (correct cmd
    shape + start_new_session=True) and NEVER runs the gate inline. Guards against
    the 2026-07-04 inline-gate death by watchdog respawn."""
    import juggle_cmd_agents_common as _com
    import juggle_cmd_agents_complete as complete_mod
    from dbops.db_topics import get_topic

    repo = "/repo"
    tid = db.create_thread("feat-topic", session_id="s")
    db.update_thread(tid, worktree_path="/wt", worktree_branch="cyc_BM",
                     main_repo_path=repo)
    _seed_running_topic(db, tid, "T-detach", repo)

    inline_gate = MagicMock()
    captured = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _AliveProc()

    with patch.object(_com.juggle_cmd_integrate, "_run_integrate", inline_gate), \
         patch.object(real_subprocess, "Popen", side_effect=_fake_popen):
        complete_mod.cmd_complete_agent(_args(tid))

    # The inline merge gate must NEVER run in the watchdog/spool process.
    inline_gate.assert_not_called()

    # A detached integrate was spawned with the correct cmd shape + detachment.
    assert captured, "complete-agent must spawn a detached integrate for a topic"
    argv = captured["argv"]
    assert argv[-2:] == ["integrate", tid]
    assert argv[1].endswith("juggle_cli.py")
    kw = captured["kwargs"]
    assert kw["start_new_session"] is True          # survives watchdog respawn
    assert kw["cwd"] == repo
    assert kw["env"]["JUGGLE_ORCHESTRATOR"] == "1"   # watchdog-owned → guard permits

    # The topic rests in 'integrating' — the reconcile tick lands it later.
    assert get_topic(db, "T-detach")["state"] == "integrating"


def test_complete_agent_returns_before_gate_finishes(db):
    """RC1 pin: cmd_complete_agent RETURNS while the detached gate is still
    running (poll() is None). The old inline path blocked here for ~7 min inside
    the watchdog and died on the next HEAD-advance respawn."""
    import juggle_cmd_agents_common as _com
    import juggle_cmd_agents_complete as complete_mod

    tid = db.create_thread("feat-topic", session_id="s")
    db.update_thread(tid, worktree_path="/wt", worktree_branch="cyc_BM",
                     main_repo_path="/repo")
    _seed_running_topic(db, tid, "T-detach2", "/repo")

    running = _AliveProc()

    with patch.object(_com.juggle_cmd_integrate, "_run_integrate",
                      side_effect=AssertionError("inline gate must not run")), \
         patch.object(real_subprocess, "Popen", return_value=running):
        complete_mod.cmd_complete_agent(_args(tid))

    # Returned normally with the gate still in flight — never awaited it.
    assert running.poll() is None


def test_already_verified_topic_does_not_crash_or_respawn(db):
    """Double-complete race: the topic is already 'verified'. start_detached_integrate
    must NOT walk it (mark_topic_integrating would raise on a terminal state) nor
    spawn a re-merge — it falls to the inline path, which is idempotent."""
    import juggle_cmd_agents_common as _com
    import juggle_cmd_agents_complete as complete_mod
    from dbops.db_topics import get_topic

    tid = db.create_thread("feat-topic", session_id="s")
    db.update_thread(tid, worktree_path="/wt", worktree_branch="cyc_BM",
                     main_repo_path="/repo")
    _seed_running_topic(db, tid, "T-verified", "/repo")
    with db._connect() as c:
        c.execute("UPDATE nodes SET state='verified' WHERE id='T-verified'")
        c.commit()

    spawned = []

    def _spy_popen(argv, **kwargs):
        spawned.append(argv)
        return _AliveProc()

    with patch.object(_com.juggle_cmd_integrate, "_run_integrate",
                      return_value=(True, "0 commits ahead")), \
         patch.object(real_subprocess, "Popen", side_effect=_spy_popen):
        complete_mod.cmd_complete_agent(_args(tid))  # must not raise

    assert spawned == [], "a verified topic must not respawn a detached integrate"
    assert get_topic(db, "T-verified")["state"] == "verified"


def test_legacy_non_topic_thread_also_detaches_never_inline(db):
    """RC2 pin (2026-07-19 stuck-in-background incident): a plain worktree
    thread with NO bound graph topic used to keep the INLINE finalize — the
    detached path was topic-scoped (autopilot) only, so an ad-hoc/interactive
    completion still ran the full fetch/rebase/test-suite/merge/push gate
    synchronously inside whatever process applied its agent_complete event.
    When that event is SPOOLED (the normal case: a dispatched coder calls
    `agent complete` from its own process), the applying process is the
    WATCHDOG's own spool-drain tick — exactly the RC1 (2026-07-04) hazard
    (self-restart on HEAD-advance / tickguard hang-kill can die mid-gate), but
    RC1's fix only ever covered topic-bound threads. A legacy thread's merge
    had nothing to retry it: no 'integrating' promotion, no reintegrate-sweep
    visibility (that sweep only scans db_topics rows) — main never moves and
    the thread sits wedged until a human runs `juggle integrate` by hand.

    Fix: finalize_or_detach_integrate now wraps ANY worktree-bound thread
    lacking a topic in a thin, pre-verified synthetic topic (one task, already
    'verified') so start_detached_integrate treats it identically to a real
    graph topic — same detached spawn, same 'integrating' promotion, same
    (already-working) reintegrate sweep lands it later. Never inline."""
    import juggle_cmd_agents_common as _com
    import juggle_cmd_agents_complete as complete_mod
    from dbops.db_topics import get_topic_by_thread

    tid = db.create_thread("adhoc", session_id="s")
    db.update_thread(tid, worktree_path="/wt", worktree_branch="cyc_X",
                     main_repo_path="/repo")  # no topic node bound

    inline_gate = MagicMock(return_value=(True, "merged"))
    captured = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _AliveProc()

    with patch.object(_com.juggle_cmd_integrate, "_run_integrate", inline_gate), \
         patch.object(real_subprocess, "Popen", side_effect=_fake_popen):
        complete_mod.cmd_complete_agent(_args(tid))

    # The inline merge gate must NEVER run for a plain thread either.
    inline_gate.assert_not_called()

    # A detached integrate was spawned, same shape as the topic path.
    assert captured, "a plain worktree thread must also get a detached integrate"
    assert captured["argv"][-2:] == ["integrate", tid]
    assert captured["kwargs"]["start_new_session"] is True

    # An auto-created wrapper topic now carries the thread, resting 'integrating'
    # so the (unmodified) reintegrate sweep picks it up on a later tick.
    topic = get_topic_by_thread(db, tid)
    assert topic is not None, "a wrapper topic must be created for a legacy thread"
    assert topic["state"] == "integrating"


def test_legacy_non_topic_thread_wrapper_does_not_double_integrate(db):
    """Double-complete race on a plain (wrapper-topic) thread, AFTER its
    detached integrate already landed (topic 'verified'): a replayed/duplicate
    agent_complete for the same thread must NOT crash nor re-spawn a second
    detached integrate (mirrors test_already_verified_topic_does_not_crash_or_
    respawn, for the RC2 auto-wrapped case instead of a real graph topic)."""
    import juggle_cmd_agents_common as _com
    import juggle_cmd_agents_complete as complete_mod
    from dbops.db_topics import get_topic_by_thread
    from juggle_cmd_agents_graph_topics import _ensure_adhoc_topic_wrapper

    tid = db.create_thread("adhoc", session_id="s")
    db.update_thread(tid, worktree_path="/wt", worktree_branch="cyc_Y",
                     main_repo_path="/repo")
    thread = db.get_thread(tid)

    # Simulate: the FIRST complete-agent already wrapped + landed the topic.
    _ensure_adhoc_topic_wrapper(db, thread, tid)
    topic = get_topic_by_thread(db, tid)
    with db._connect() as c:
        c.execute("UPDATE nodes SET state='verified' WHERE id=?", (topic["id"],))
        c.commit()

    spawned = []

    def _spy_popen(argv, **kwargs):
        spawned.append(argv)
        return _AliveProc()

    with patch.object(_com.juggle_cmd_integrate, "_run_integrate",
                      return_value=(True, "0 commits ahead")), \
         patch.object(real_subprocess, "Popen", side_effect=_spy_popen):
        complete_mod.cmd_complete_agent(_args(tid))  # must not raise

    assert spawned == [], "an already-landed wrapper topic must not respawn integrate"
    assert get_topic_by_thread(db, tid)["state"] == "verified"


def test_plain_thread_without_worktree_unaffected(db):
    """No worktree fields (pre-migration/no-op thread) — no topic wrapper, no
    spawn; falls straight to the bare _finalize_worktree no-worktree path."""
    import juggle_cmd_agents_common as _com
    import juggle_cmd_agents_complete as complete_mod
    from dbops.db_topics import get_topic_by_thread

    tid = db.create_thread("no-worktree", session_id="s")

    def _no_spawn(argv, **kwargs):
        raise AssertionError("no worktree — nothing to integrate, must not spawn")

    with patch.object(real_subprocess, "Popen", side_effect=_no_spawn):
        complete_mod.cmd_complete_agent(_args(tid))

    assert get_topic_by_thread(db, tid) is None
