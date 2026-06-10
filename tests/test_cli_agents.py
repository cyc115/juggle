"""CLI tests: agent pool commands, send-task/send-message, harness gate (split from test_juggle_cli.py, 2026-06-10)."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SRC_DIR_TOP = str(Path(__file__).parent.parent / "src")
if SRC_DIR_TOP not in sys.path:
    sys.path.insert(0, SRC_DIR_TOP)
from juggle_cli import _last_sentences

CLI = str(Path(__file__).parent.parent / "src" / "juggle_cli.py")
SRC_DIR = str(Path(__file__).parent.parent / "src")


def run_cli(args, db_path):
    """Run juggle_cli.py with a test DB path, override DB_PATH via env."""
    import os

    env = os.environ.copy()
    # Patch by passing db_path via a temporary monkeypatch in subprocess
    result = subprocess.run(
        [sys.executable, CLI] + args,
        capture_output=True,
        text=True,
        env={**env, "_JUGGLE_TEST_DB": str(db_path)},
    )
    return result


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def started_db(tmp_path):
    """Return (db_path, general_thread_uuid) after running `start`."""
    db_path = tmp_path / "test.db"
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db = JuggleDB(str(db_path))
    db.init_db()
    db.set_active(True)
    tid = db.create_thread("General", session_id="")
    db.set_current_thread(tid)
    return db_path, tid


# ------------------------------------------------------------------
# Agent pool CLI tests (Tasks 6–10)
# ------------------------------------------------------------------


def patch_tmux_spawn(pane_id="%1"):
    """Set env var so juggle_tmux uses a mock pane instead of real tmux."""
    return patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": pane_id})


def test_spawn_agent_creates_agent(started_db):
    """spawn-agent should print agent_id and pane_id."""
    db_path, _ = started_db
    with patch_tmux_spawn(pane_id="%7"):
        result = run_cli(["spawn-agent", "coder"], db_path)
    assert result.returncode == 0
    parts = result.stdout.strip().split()
    assert len(parts) == 2  # agent_id, pane_id
    assert parts[1] == "%7"


def test_list_agents_empty(started_db):
    db_path, _ = started_db
    result = run_cli(["list-agents"], db_path)
    assert result.returncode == 0
    assert "No agents" in result.stdout


def test_list_agents_shows_agents(started_db):
    db_path, _ = started_db
    with patch_tmux_spawn(pane_id="%2"):
        run_cli(["spawn-agent", "researcher"], db_path)
    result = run_cli(["list-agents"], db_path)
    assert "researcher" in result.stdout
    assert "%2" in result.stdout


def test_get_agent_spawns_when_pool_empty(started_db):
    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%5"}):
        result = run_cli(["get-agent", thread_id], db_path)
    assert result.returncode == 0
    parts = result.stdout.strip().split()
    assert len(parts) == 3  # agent_id, pane_id, "new"
    assert parts[1] == "%5"
    assert parts[2] == "new"


def test_get_agent_reuses_idle_agent(started_db):
    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        run_cli(["spawn-agent", "coder"], db_path)
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_NOT_READY_PANES": ""}):
        result = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
    assert result.returncode == 0
    parts = result.stdout.strip().split()
    assert len(parts) == 2  # agent_id, pane_id (no "new")
    assert parts[1] == "%3"


def test_get_agent_marks_busy(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        run_cli(["spawn-agent", "coder"], db_path)
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_NOT_READY_PANES": ""}):
        result = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)

    agent_id = result.stdout.strip().split()[0]
    db = JuggleDB(str(db_path))
    agent = db.get_agent(agent_id)
    assert agent is not None
    assert agent["status"] == "busy"
    assert agent["assigned_thread"] == thread_id


def test_get_agent_skips_non_ready_idle_and_spawns(started_db):
    """An idle agent whose pane is NOT ready must be skipped — spawn new."""
    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        run_cli(["spawn-agent", "coder"], db_path)
    # Mark %3 as NOT ready; new spawns get %5
    with patch.dict(os.environ, {
        "JUGGLE_TMUX_MOCK_NOT_READY_PANES": "%3",
        "JUGGLE_TMUX_MOCK_PANE": "%5",
    }):
        result = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
    assert result.returncode == 0
    parts = result.stdout.strip().split()
    assert len(parts) == 3  # agent_id, pane_id, "new"
    assert parts[1] == "%5", f"expected new pane %5, got {parts[1]}"
    assert parts[2] == "new"


def test_get_agent_reuses_ready_idle_no_spawn(started_db):
    """A ready idle agent must be reused — no new spawn."""
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["spawn-agent", "coder"], db_path)
    first_agent_id = r.stdout.strip().split()[0]

    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_NOT_READY_PANES": ""}):
        result = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
    assert result.returncode == 0
    parts = result.stdout.strip().split()
    assert len(parts) == 2  # agent_id, pane_id (no "new")
    assert parts[1] == "%3"
    assert parts[0] == first_agent_id  # exact same agent reused

    db = JuggleDB(str(db_path))
    agent = db.get_agent(first_agent_id)
    assert agent is not None
    assert agent["status"] == "busy"
    assert agent["assigned_thread"] == thread_id


def test_get_agent_stdout_contract_preserved(started_db):
    """Stdout format must be exactly '<uuid> <pane>[ new]'."""
    db_path, thread_id = started_db
    # New agent path
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%9"}):
        result = run_cli(["get-agent", thread_id], db_path)
    assert result.returncode == 0
    parts_new = result.stdout.strip().split()
    assert len(parts_new) == 3
    assert parts_new[2] == "new"
    # Verify UUID format (36 chars, 4 dashes)
    assert len(parts_new[0]) == 36
    assert parts_new[0].count("-") == 4

    # Reused agent path
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db = JuggleDB(str(db_path))
    db.update_agent(parts_new[0], status="idle", assigned_thread=None)
    # Start a second thread so we can reuse
    tid2 = db.create_thread("Second", session_id="")
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_NOT_READY_PANES": ""}):
        result2 = run_cli(["get-agent", tid2, "--role", "researcher"], db_path)
    assert result2.returncode == 0
    parts_reuse = result2.stdout.strip().split()
    assert len(parts_reuse) == 2  # no "new"
    assert len(parts_reuse[0]) == 36
    assert parts_reuse[0].count("-") == 4


def test_release_agent_marks_idle(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db_path, thread_id = started_db
    with patch.dict(os.environ, {
        "JUGGLE_TMUX_MOCK_PANE": "%3",
        "JUGGLE_TMUX_MOCK_NOT_READY_PANES": "",
    }):
        r = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
        agent_id = r.stdout.strip().split()[0]
        result = run_cli(["release-agent", agent_id, "--force"], db_path)
    assert result.returncode == 0

    db = JuggleDB(str(db_path))
    agent = db.get_agent(agent_id)
    assert agent is not None
    assert agent["status"] == "idle"
    assert agent["assigned_thread"] is None


def test_release_agent_adds_context_thread(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db_path, thread_id = started_db
    with patch.dict(os.environ, {
        "JUGGLE_TMUX_MOCK_PANE": "%3",
        "JUGGLE_TMUX_MOCK_NOT_READY_PANES": "",
    }):
        r = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
        agent_id = r.stdout.strip().split()[0]
        run_cli(["release-agent", agent_id, "--force"], db_path)

    db = JuggleDB(str(db_path))
    agent = db.get_agent(agent_id)
    assert agent is not None
    context = json.loads(agent["context_threads"])
    assert thread_id in context


def test_release_agent_decommissions_pending(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db_path, thread_id = started_db
    with patch.dict(os.environ, {
        "JUGGLE_TMUX_MOCK_PANE": "%3",
        "JUGGLE_TMUX_MOCK_NOT_READY_PANES": "",
    }):
        r = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]

    db = JuggleDB(str(db_path))
    db.update_agent(agent_id, status="decommission_pending")

    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_KILL": "1"}):
        result = run_cli(["release-agent", agent_id], db_path)
    assert result.returncode == 0
    assert db.get_agent(agent_id) is None


def test_decommission_agent_removes_from_db(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db_path, _ = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["spawn-agent", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]

    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_KILL": "1"}):
        result = run_cli(["decommission-agent", agent_id], db_path)
    assert result.returncode == 0
    assert JuggleDB(str(db_path)).get_agent(agent_id) is None


def test_send_task_appends_release_and_sends(started_db, tmp_path):
    """send-task should append release-agent call and invoke send_task on manager."""
    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]

    prompt_file = tmp_path / "task.txt"
    prompt_file.write_text(
        "Do the thing.\npython3 juggle_cli.py complete-agent X result\n"
    )

    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_SEND": "1"}):
        result = run_cli(["send-task", agent_id, str(prompt_file)], db_path)
    assert result.returncode == 0
    assert "sent" in result.stdout.lower()


def test_send_task_error_on_missing_prompt_file(started_db):
    db_path, _ = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["spawn-agent", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]

    result = run_cli(["send-task", agent_id, "/nonexistent/task.txt"], db_path)
    assert result.returncode == 1
    assert "not found" in result.stdout.lower()


def test_send_task_prepends_universal_preamble(started_db, tmp_path):
    """UNIVERSAL_PREAMBLE must be prepended to the task body stored in last_task."""
    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]

    prompt_file = tmp_path / "task.txt"
    prompt_file.write_text("Do the thing.\n")

    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_SEND": "1"}):
        result = run_cli(["send-task", agent_id, str(prompt_file)], db_path)
    assert result.returncode == 0

    sys.path.insert(0, SRC_DIR)
    from juggle_cmd_agents import UNIVERSAL_PREAMBLE
    from juggle_db import JuggleDB

    db = JuggleDB(str(db_path))
    agent = db.get_agent(agent_id)
    assert agent["last_task"].startswith(UNIVERSAL_PREAMBLE)
    assert "Do the thing." in agent["last_task"]


def test_archive_thread_decommissions_idle_agents(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db_path, thread_id = started_db
    # Spawn agent and assign to thread
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]
    # Release so it's idle (still has assigned_thread in context_threads)
    run_cli(["release-agent", agent_id], db_path)

    # Manually set assigned_thread back so archive-thread can find it
    db = JuggleDB(str(db_path))
    db.update_agent(agent_id, assigned_thread=thread_id, status="idle")

    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_KILL": "1"}):
        result = run_cli(["archive-thread", thread_id], db_path)
    assert result.returncode == 0
    assert JuggleDB(str(db_path)).get_agent(agent_id) is None


def test_archive_thread_marks_busy_agents_pending(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
        agent_id = r.stdout.strip().split()[0]
    # Agent is busy (still assigned); archive-thread doesn't need mock
    result = run_cli(["archive-thread", thread_id], db_path)
    assert result.returncode == 0
    agent = JuggleDB(str(db_path)).get_agent(agent_id)
    assert agent is not None
    assert agent["status"] == "decommission_pending"



# ── send-message command ─────────────────────────────────────────────────────

def test_send_message_cli_success(started_db):
    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]

    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_SEND": "1"}):
        result = run_cli(["send-message", agent_id, "please handle the edge case"], db_path)
    assert result.returncode == 0
    assert "sent" in result.stdout.lower()


def test_send_message_cli_json_output(started_db):
    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]

    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_SEND": "1"}):
        result = run_cli(["send-message", "--json", agent_id, "steer this"], db_path)
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["agent_id"] == agent_id


def test_send_message_cli_error_missing_agent(started_db):
    db_path, _ = started_db
    result = run_cli(["send-message", "nonexistent-id", "hello"], db_path)
    assert result.returncode == 1


# ── harness gate in coder template ───────────────────────────────────────────

def test_send_task_coder_template_includes_harness_gate(started_db, tmp_path):
    """send-task with coder role must inject HARNESS GATE into the prompt."""
    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]

    prompt_file = tmp_path / "task.txt"
    prompt_file.write_text("Do the thing.\n")

    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_SEND": "1"}):
        run_cli(["send-task", agent_id, str(prompt_file)], db_path)

    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    agent = JuggleDB(str(db_path)).get_agent(agent_id)
    assert "HARNESS GATE" in agent["last_task"]


def test_send_task_no_template_bypasses_harness_gate(started_db, tmp_path):
    """--no-template must skip harness gate injection."""
    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]

    prompt_file = tmp_path / "task.txt"
    prompt_file.write_text("Do the thing.\n")

    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_SEND": "1"}):
        run_cli(["send-task", "--no-template", agent_id, str(prompt_file)], db_path)

    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    agent = JuggleDB(str(db_path)).get_agent(agent_id)
    assert "HARNESS GATE" not in agent["last_task"]
