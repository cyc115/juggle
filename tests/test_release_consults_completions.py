"""Regression pin: agent release consults the recorded completion ledger.

Incident 2026-07-03/04 (integrate-wedge #2, RC3 of 4): `agent release --force`
on a topic thread whose completion WAS already recorded still marked the topic
failed and filed spurious HIGH escalations, because the reconcile arm decided
"released without completing" purely from thread state
(juggle_cmd_agents_lifecycle.py) and never consulted agent_completions /
spool-applied agent_complete (recorded in the durable agent_runs ledger).

Fix: before the "released without completing" arm, check whether a completion
for the assigned thread is recorded (an agent_runs row in status='completed').
If yes → routine post-completion cleanup: no failed mark, no HIGH action item,
INFO notification only.
"""

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
    return d


def _record_completion(db, thread_id, agent_id):
    """Simulate the completion ledger row a spool-applied agent_complete leaves:
    a dispatched agent_runs row closed with status='completed' via close_run."""
    db.insert_agent_run(
        thread_id=thread_id,
        input_prompt="do the thing",
        agent_id=agent_id,
        role="coder",
        model="claude",
        harness="claude",
        project_id=None,
        topic_id=None,
        task_id=None,
    )
    db.close_run(thread_id, output="done", diffstat=None, status="completed")


def test_force_release_after_recorded_completion_is_routine_cleanup(db):
    """RED first: completion recorded → force-release must NOT mark failed and
    must NOT file a HIGH failure action item."""
    from juggle_cmd_agents import cmd_release_agent

    tid = db.create_thread("done-topic", session_id="s")
    db.update_thread(tid, status="background")
    agent_id = db.create_agent("coder", "pane-1")
    db.update_agent(agent_id, status="busy", assigned_thread=tid)

    _record_completion(db, tid, agent_id)

    cmd_release_agent(argparse.Namespace(agent_id=agent_id, force=True))

    items = db.get_open_action_items()
    assert not any(
        item["type"] == "failure" and "released without completing" in item["message"]
        for item in items
    ), f"completed thread should not file a failure action item, got: {items}"
    assert db.get_thread(tid)["state"] != "failed-exec"


def test_force_release_stale_completion_then_stuck_redispatch_still_fails(db):
    """A completed run from a PRIOR dispatch must not mask a genuinely-stuck
    re-dispatch: the newest run is still open ('dispatched'), so this is a
    failure, not routine cleanup."""
    from juggle_cmd_agents import cmd_release_agent

    tid = db.create_thread("redispatch-topic", session_id="s")
    db.update_thread(tid, status="background")
    agent_id = db.create_agent("coder", "pane-3")
    db.update_agent(agent_id, status="busy", assigned_thread=tid)

    # Prior dispatch completed...
    _record_completion(db, tid, agent_id)
    # ...then re-dispatched and left open (never completed).
    db.insert_agent_run(
        thread_id=tid, input_prompt="retry", agent_id=agent_id, role="coder",
        model="claude", harness="claude", project_id=None, topic_id=None, task_id=None,
    )

    cmd_release_agent(argparse.Namespace(agent_id=agent_id, force=True))

    items = db.get_open_action_items()
    assert any(
        item["type"] == "failure" and "released without completing" in item["message"]
        for item in items
    ), f"stuck re-dispatch should still file a failure item, got: {items}"
    assert db.get_thread(tid)["state"] == "failed-exec"


def test_force_release_without_completion_still_fails(db):
    """No completion recorded → existing failed-path behavior is unchanged."""
    from juggle_cmd_agents import cmd_release_agent

    tid = db.create_thread("stuck-topic", session_id="s")
    db.update_thread(tid, status="background")
    agent_id = db.create_agent("coder", "pane-2")
    db.update_agent(agent_id, status="busy", assigned_thread=tid)

    cmd_release_agent(argparse.Namespace(agent_id=agent_id, force=True))

    items = db.get_open_action_items()
    assert any(
        item["priority"] == "high"
        and item["type"] == "failure"
        and "released without completing" in item["message"]
        for item in items
    ), f"Expected failure action item, got: {items}"
    assert db.get_thread(tid)["state"] == "failed-exec"
