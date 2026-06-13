"""Integration tests: the ledger wires into the dispatch and completion
choke points (send-task → insert run; complete-agent / mark_graph_topic →
close run). Mirrors the subprocess CLI harness used by test_cli_agents.py.
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

CLI = str(Path(__file__).parent.parent / "src" / "juggle_cli.py")


def run_cli(args, db_path):
    env = os.environ.copy()
    return subprocess.run(
        [sys.executable, CLI] + args,
        capture_output=True,
        text=True,
        env={**env, "_JUGGLE_TEST_DB": str(db_path)},
    )


@pytest.fixture
def started_db(tmp_path):
    db_path = tmp_path / "test.db"
    from juggle_db import JuggleDB

    db = JuggleDB(str(db_path))
    db.init_db()
    db.set_active(True)
    tid = db.create_thread("General", session_id="")
    db.set_current_thread(tid)
    return db_path, tid


def _dispatch(db_path, thread_id, prompt_file, role="coder"):
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["get-agent", thread_id, "--role", role], db_path)
    agent_id = r.stdout.strip().split()[0]
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_SEND": "1"}):
        res = run_cli(["send-task", agent_id, str(prompt_file)], db_path)
    return agent_id, res


# ---------------------------------------------------------------------------
# DISPATCH: a run row is created with input_prompt == full_prompt
# ---------------------------------------------------------------------------


def test_dispatch_creates_run_inbox_thread(started_db, tmp_path):
    db_path, thread_id = started_db
    prompt_file = tmp_path / "task.txt"
    prompt_file.write_text("Do the thing.\n")

    agent_id, res = _dispatch(db_path, thread_id, prompt_file)
    assert res.returncode == 0

    from juggle_db import JuggleDB

    db = JuggleDB(str(db_path))
    agent = db.get_agent(agent_id)
    runs = db.get_runs(thread_id=thread_id)
    assert len(runs) == 1
    run = runs[0]
    # INPUT is byte-faithful to what the agent received.
    assert run["input_prompt"] == agent["last_task"]
    assert run["status"] == "dispatched"
    # INBOX (non-project) thread defaults project_id=INBOX, no topic/node.
    assert run["project_id"] == "INBOX"
    assert run["topic_id"] is None
    assert run["task_id"] is None
    # current_run_id correlation set on the agent.
    assert str(agent["current_run_id"]) == str(run["id"])


def test_dispatch_resolves_graph_node_thread(started_db, tmp_path):
    """A graph-task-bound thread records project_id + task_id on the run."""
    db_path, _ = started_db
    from dbops import db_graph
    from juggle_db import JuggleDB

    db = JuggleDB(str(db_path))
    # Create a project + graph node bound to a fresh thread.
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, objective, status, created_at, last_active) "
            "VALUES ('PG','PG','obj','active',datetime('now'),datetime('now'))"
        )
        conn.commit()
    gthread = db.create_thread("graph thread", session_id="")
    db.update_thread(gthread, project_id="PG")
    db_graph.create_task(
        db, task_id="N1", project_id="PG", title="t", prompt="p",
        verify_cmd=None,
    )
    db_graph.set_task_thread(db, "N1", gthread)

    prompt_file = tmp_path / "task.txt"
    prompt_file.write_text("graph task.\n")
    # Bound graph task is tick-owned; dispatch with --force-task.
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%4"}):
        r = run_cli(["get-agent", gthread, "--role", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_SEND": "1"}):
        res = run_cli(
            ["send-task", "--force-task", agent_id, str(prompt_file)], db_path
        )
    assert res.returncode == 0, res.stdout + res.stderr

    runs = JuggleDB(str(db_path)).get_runs(thread_id=gthread)
    assert len(runs) == 1
    assert runs[0]["project_id"] == "PG"
    assert runs[0]["task_id"] == "N1"


# ---------------------------------------------------------------------------
# COMPLETE: the matching run is closed with output set
# ---------------------------------------------------------------------------


def test_complete_agent_closes_run(started_db, tmp_path):
    db_path, thread_id = started_db
    prompt_file = tmp_path / "task.txt"
    prompt_file.write_text("Do the thing.\n")
    _dispatch(db_path, thread_id, prompt_file)

    res = run_cli(
        ["complete-agent", thread_id, "All done — shipped it."], db_path
    )
    assert res.returncode == 0, res.stdout + res.stderr

    from juggle_db import JuggleDB

    runs = JuggleDB(str(db_path)).get_runs(thread_id=thread_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["output"] == "All done — shipped it."
    assert runs[0]["completed_at"]
