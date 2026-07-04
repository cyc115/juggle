"""Fixes 3 & 4 — completion effects must not be conditional on a live binding,
and a finalization-failure action item must survive.

Incident (2026-07-03 integrate-wedge RCA):
* Q3 — all 5 pool agents stuck 'busy' and 2 researchers stuck 'busy' even after
  CLEAN completions: cmd_complete_agent reaped the pool agent + closed the
  ledger ONLY via get_agent_by_thread (status='busy' AND assigned_thread=thread).
  In spool-replay-after-rebind that binding had shifted, so the idle-write
  silently no-op'd and the run stayed 'dispatched'. Fix 3: reap by the run's
  recorded agent_id and close the ledger by thread_id, independent of the live
  binding.
* Fix 4 — the finalization-failure action item (created BEFORE the
  items_to_dismiss snapshot) was itself swept into that snapshot and
  auto-dismissed, silently swallowing a genuine integrate failure. Snapshot
  BEFORE creating any new items.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB


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
        open_questions=None, handoff=None, role="coder",
    )


# ── Fix 4: finalization-failure item survives the auto-dismiss sweep ─────────


def test_finalization_failure_item_survives(db, monkeypatch):
    import juggle_cmd_agents_common as _com
    import juggle_cmd_agents_complete as complete_mod

    tid = db.create_thread("feat-topic", session_id="s")
    # Worktree fields present → routes through _run_integrate (which we fail).
    db.update_thread(tid, worktree_path="/wt", worktree_branch="cyc_X",
                     main_repo_path="/repo")

    monkeypatch.setattr(_com.juggle_cmd_integrate, "_run_integrate",
                        lambda thread, db_: (False, "rebase conflict"))

    complete_mod.cmd_complete_agent(_args(tid))

    items = db.get_open_action_items()
    assert any(
        i["priority"] == "high" and "finalization failed" in i["message"]
        for i in items
    ), f"finalization-failure item must survive, got: {items}"


# ── Fix 3: reap the pool agent + close the ledger without a live binding ─────


def test_agent_reaped_by_recorded_run_when_binding_moved(db, monkeypatch):
    """get_agent_by_thread misses (agent busy but not assigned to this thread) —
    the agent recorded on the thread's open ledger run must still be reaped
    idle, and the run closed by thread_id."""
    import juggle_cmd_agents_complete as complete_mod

    tid = db.create_thread("feat-topic", session_id="s")  # plain thread, no worktree
    agent_id = db.create_agent(role="coder", pane_id="pane-1")
    # Busy but UNassigned (leaked binding) → get_agent_by_thread(tid) == None.
    db.update_agent(agent_id, status="busy", assigned_thread=None)
    assert db.get_agent_by_thread(tid) is None
    run_id = db.insert_agent_run(
        thread_id=tid, input_prompt="do it", agent_id=agent_id, role="coder",
        model=None, harness=None, project_id=None, topic_id=None, task_id=None,
    )

    complete_mod.cmd_complete_agent(_args(tid))

    reaped = db.get_agent(agent_id)
    assert reaped["status"] == "idle", "leaked-busy agent must be reaped via the run's agent_id"
    assert reaped["assigned_thread"] is None
    assert db.get_run(run_id)["status"] != "dispatched", "ledger run must be closed by thread_id"


def test_reap_never_idles_agent_busy_on_another_thread(db, monkeypatch):
    """Safety: the recorded agent must NOT be idled if it has since been
    re-dispatched and is busy on a DIFFERENT thread."""
    import juggle_cmd_agents_complete as complete_mod

    tid = db.create_thread("feat-topic", session_id="s")
    other = db.create_thread("other-topic", session_id="s")
    agent_id = db.create_agent(role="coder", pane_id="pane-1")
    db.update_agent(agent_id, status="busy", assigned_thread=other)  # working elsewhere
    db.insert_agent_run(
        thread_id=tid, input_prompt="do it", agent_id=agent_id, role="coder",
        model=None, harness=None, project_id=None, topic_id=None, task_id=None,
    )

    complete_mod.cmd_complete_agent(_args(tid))

    still = db.get_agent(agent_id)
    assert still["status"] == "busy", "agent working on another thread must be left alone"
    assert still["assigned_thread"] == other
