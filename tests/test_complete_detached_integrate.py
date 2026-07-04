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


def test_legacy_non_topic_thread_still_finalizes_inline(db):
    """A plain worktree thread with NO bound graph topic keeps the inline
    finalize — the detached path is topic-scoped (autopilot), so interactive
    completions are unaffected."""
    import juggle_cmd_agents_common as _com
    import juggle_cmd_agents_complete as complete_mod

    tid = db.create_thread("adhoc", session_id="s")
    db.update_thread(tid, worktree_path="/wt", worktree_branch="cyc_X",
                     main_repo_path="/repo")  # no topic node bound

    inline_gate = MagicMock(return_value=(True, "merged"))

    def _no_spawn(argv, **kwargs):
        raise AssertionError("legacy thread must not spawn a detached integrate")

    with patch.object(_com.juggle_cmd_integrate, "_run_integrate", inline_gate), \
         patch.object(real_subprocess, "Popen", side_effect=_no_spawn):
        complete_mod.cmd_complete_agent(_args(tid))

    inline_gate.assert_called_once()
