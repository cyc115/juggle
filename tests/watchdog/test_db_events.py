"""Tests for watchdog-related DB schema and methods."""

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


def _col_names(db, table):
    with db._connect() as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


# --- Migration 20: agents columns ---


def test_agents_has_watchdog_retried(db):
    assert "watchdog_retried" in _col_names(db, "agents")


def test_agents_has_watchdog_threshold_minutes(db):
    assert "watchdog_threshold_minutes" in _col_names(db, "agents")


def test_agents_has_model(db):
    assert "model" in _col_names(db, "agents")


def test_agents_has_last_task(db):
    assert "last_task" in _col_names(db, "agents")


def test_agents_has_busy_since(db):
    assert "busy_since" in _col_names(db, "agents")


def test_agents_has_last_send_task_pane_hash(db):
    assert "last_send_task_pane_hash" in _col_names(db, "agents")


def test_agents_has_last_send_task_at(db):
    assert "last_send_task_at" in _col_names(db, "agents")


def test_agents_has_last_activity_at(db):
    assert "last_activity_at" in _col_names(db, "agents")


# --- Migration 22: threads columns ---


def test_threads_has_last_dispatched_columns(db):
    """P8 terminal: the last_dispatched_* columns live on the conversation node now
    (legacy threads table dropped, Migration 55)."""
    cols = _col_names(db, "nodes")
    assert "last_dispatched_task" in cols
    assert "last_dispatched_role" in cols
    assert "last_dispatched_model" in cols


# --- Tables ---


def test_agent_completions_table_exists(db):
    with db._connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "agent_completions" in tables


def test_watchdog_events_table_exists(db):
    with db._connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "watchdog_events" in tables


# --- DB methods ---


def test_insert_agent_completion(db):
    db.insert_agent_completion(role="coder", duration_secs=120.5)
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM agent_completions").fetchone()
    assert row["role"] == "coder"
    assert abs(row["duration_secs"] - 120.5) < 0.01


def test_get_median_coldstart(db):
    db.insert_agent_completion(role="coder", duration_secs=100.0)
    assert db.get_median_duration_secs("coder") is None  # < 10 samples


def test_get_median_adaptive(db):
    for i in range(10):
        db.insert_agent_completion(role="coder", duration_secs=float(100 + i * 10))
    median = db.get_median_duration_secs("coder")
    assert median is not None
    assert abs(median - 145.0) < 0.01


def test_add_watchdog_event(db):
    db.add_watchdog_event(
        agent_id="test-agent-id",
        thread_id=None,
        event_type="stalled",
        snapshot_path="/tmp/snap.txt",
    )
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM watchdog_events").fetchone()
    assert row["agent_id"] == "test-agent-id"
    assert row["event_type"] == "stalled"


# --- Task 2 tests: metadata tracking ---


def test_get_agent_sets_busy_since(tmp_path):
    import os, subprocess, sys

    db_path = tmp_path / "test.db"
    d = JuggleDB(str(db_path))
    d.init_db()
    thread_id = d.create_thread("test topic", session_id="")
    agent_id_raw = d.create_agent(role="coder", pane_id="%5")
    d.update_agent(agent_id_raw, status="idle")

    result = subprocess.run(
        [
            sys.executable,
            "src/juggle_cli.py",
            "agent", "get",
            thread_id,
            "--role",
            "coder",
            "--model",
            "claude-sonnet-4-6",
        ],
        cwd=str(Path(__file__).parent.parent.parent),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "_JUGGLE_TEST_DB": str(db_path),
            "JUGGLE_TMUX_MOCK_PANE": "%5",
        },
    )
    agent_id = result.stdout.strip().split()[0]
    agent = d.get_agent(agent_id)
    assert agent is not None, (
        f"Agent not found; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert agent["busy_since"] is not None
    assert agent["model"] == "claude-sonnet-4-6"


def test_send_task_stores_last_task_and_pane_hash(tmp_path):
    """send-task writes last_task, last_send_task_pane_hash (16 hex chars), last_send_task_at."""
    import os, subprocess, sys

    db_path = tmp_path / "test.db"
    task_file = tmp_path / "task.txt"
    task_file.write_text("do something useful")

    d = JuggleDB(str(db_path))
    d.init_db()
    agent_id = d.create_agent(role="coder", pane_id="%5")
    d.update_agent(agent_id, status="busy")

    subprocess.run(
        [sys.executable, "src/juggle_cli.py", "agent", "send-task", agent_id, str(task_file)],
        cwd=str(Path(__file__).parent.parent.parent),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "_JUGGLE_TEST_DB": str(db_path),
            "JUGGLE_TMUX_MOCK_SEND": "1",
            "JUGGLE_TMUX_MOCK_PANE": "%5",
        },
    )
    agent = d.get_agent(agent_id)
    assert agent["last_task"].endswith("do something useful")
    assert agent["last_send_task_pane_hash"] is not None
    assert len(agent["last_send_task_pane_hash"]) == 16
    assert agent["last_send_task_at"] is not None


def test_complete_agent_inserts_completion(tmp_path):
    import os, subprocess, sys

    db_path = tmp_path / "test.db"

    d = JuggleDB(str(db_path))
    d.init_db()
    thread_id = d.create_thread("test topic", session_id="")
    agent_id = d.create_agent(role="coder", pane_id="%5")
    busy_since = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    d.update_agent(
        agent_id, status="busy", assigned_thread=thread_id, busy_since=busy_since
    )

    subprocess.run(
        [
            sys.executable,
            "src/juggle_cli.py",
            "agent", "complete",
            thread_id,
            "Done. All good.",
        ],
        cwd=str(Path(__file__).parent.parent.parent),
        capture_output=True,
        text=True,
        env={**os.environ, "_JUGGLE_TEST_DB": str(db_path)},
    )
    with d._connect() as conn:
        rows = conn.execute("SELECT * FROM agent_completions").fetchall()
    assert len(rows) == 1
    assert rows[0]["role"] == "coder"
    assert rows[0]["duration_secs"] >= 100


def test_release_agent_copies_dispatch_payload(tmp_path):
    """release-agent copies last_task/role/model to thread before decommissioning agent."""
    import argparse

    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()

    import juggle_cli_common as common
    import juggle_cmd_agents_common

    original_get_db_common = common.get_db
    original_get_db_agents = juggle_cmd_agents_common.get_db
    common.get_db = lambda: d
    juggle_cmd_agents_common.get_db = lambda: d

    thread_id = d.create_thread("payload test", session_id="")
    d.update_thread(thread_id, status="background")
    agent_id = d.create_agent(role="coder", pane_id="%5")
    d.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="do the thing",
        model="claude-sonnet-4-6",
    )

    from juggle_cmd_agents import cmd_release_agent

    args = argparse.Namespace(agent_id=agent_id, force=True)
    cmd_release_agent(args)

    thread = d.get_thread(thread_id)
    assert thread["last_dispatched_task"] == "do the thing"
    assert thread["last_dispatched_role"] == "coder"
    assert thread["last_dispatched_model"] == "claude-sonnet-4-6"

    common.get_db = original_get_db_common
    juggle_cmd_agents_common.get_db = original_get_db_agents


# --- Task 7 tests: set-watchdog CLI ---


def test_set_watchdog_minutes(tmp_path):
    import os, subprocess, sys

    db_path = tmp_path / "test.db"
    d = JuggleDB(str(db_path))
    d.init_db()
    agent_id = d.create_agent(role="coder", pane_id="%5")

    result = subprocess.run(
        [sys.executable, "src/juggle_cli.py", "agent", "set-watchdog", agent_id, "15"],
        cwd=str(Path(__file__).parent.parent.parent),
        capture_output=True,
        text=True,
        env={**os.environ, "_JUGGLE_TEST_DB": str(db_path)},
    )
    assert result.returncode == 0, result.stderr
    assert d.get_agent(agent_id)["watchdog_threshold_minutes"] == 15


def test_set_watchdog_off(tmp_path):
    import os, subprocess, sys

    db_path = tmp_path / "test.db"
    d = JuggleDB(str(db_path))
    d.init_db()
    agent_id = d.create_agent(role="coder", pane_id="%5")

    subprocess.run(
        [sys.executable, "src/juggle_cli.py", "agent", "set-watchdog", agent_id, "off"],
        cwd=str(Path(__file__).parent.parent.parent),
        capture_output=True,
        text=True,
        env={**os.environ, "_JUGGLE_TEST_DB": str(db_path)},
    )
    assert d.get_agent(agent_id)["watchdog_threshold_minutes"] == -1
