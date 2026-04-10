"""Tests for juggle_cli.py using subprocess."""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

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


def test_init_db(db_path):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    db.init_db()
    assert db_path.exists()


def test_show_topics_empty(started_db):
    db_path, general_tid = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    threads = db.get_all_threads()
    assert len(threads) == 1
    assert threads[0]["id"] == general_tid
    assert threads[0]["label"] == "A"


def test_create_thread(started_db):
    db_path, _ = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    tid = db.create_thread("New topic", session_id="")
    # New thread should be a UUID, not "B"
    assert len(tid) > 1
    thread = db.get_thread(tid)
    assert thread is not None
    assert thread["label"] == "B"


def test_switch_thread(started_db):
    db_path, _ = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    second_tid = db.create_thread("Second topic", session_id="")
    db.set_current_thread(second_tid)
    assert db.get_current_thread() == second_tid


def test_update_thread_meta(started_db):
    db_path, general_tid = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    db.update_thread(general_tid, key_decisions=["Use SQLite"])
    t = db.get_thread(general_tid)
    assert t is not None
    decisions = json.loads(t["key_decisions"])
    assert "Use SQLite" in decisions


def test_update_summary(started_db):
    db_path, general_tid = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    db.update_thread(general_tid, summary="We decided to use juggle.")
    t = db.get_thread(general_tid)
    assert t is not None
    assert t["summary"] == "We decided to use juggle."


def test_close_thread(started_db):
    db_path, general_tid = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    db.update_thread(general_tid, status="closed")
    t = db.get_thread(general_tid)
    assert t is not None
    assert t["status"] == "closed"


def test_add_shared(started_db):
    db_path, general_tid = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    db.add_shared("decision", "Use JWT", source_thread=general_tid)
    shared = db.get_shared_context()
    assert len(shared) == 1


def test_set_and_check_agent(started_db):
    db_path, general_tid = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    db.update_thread(general_tid, agent_task_id="task_abc", status="background")
    agents = db.get_background_agents()
    assert len(agents) == 1
    assert agents[0]["agent_task_id"] == "task_abc"


def test_complete_agent(started_db):
    db_path, general_tid = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    db.update_thread(general_tid, agent_task_id="task_abc", status="background")
    db.update_thread(general_tid, agent_result="Done", status="done")
    db.add_notification(general_tid, "Topic A complete")
    t = db.get_thread(general_tid)
    assert t is not None
    assert t["status"] == "done"
    pending = db.get_pending_notifications()
    assert len(pending) == 1


def test_fail_agent(started_db):
    db_path, general_tid = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    db.update_thread(general_tid, status="failed", agent_result="timeout")
    t = db.get_thread(general_tid)
    assert t is not None
    assert t["status"] == "failed"


def test_set_summarized_count(started_db):
    db_path, general_tid = started_db
    result = run_cli(["set-summarized-count", "A", "5"], db_path)
    assert result.returncode == 0
    assert "5" in result.stdout

    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    thread = db.get_thread(general_tid)
    assert thread is not None
    assert thread["summarized_msg_count"] == 5


def test_get_stale_threads_empty(started_db):
    db_path, _ = started_db
    result = run_cli(["get-stale-threads"], db_path)
    assert result.returncode == 0
    assert "No stale" in result.stdout


def test_get_stale_threads_finds_stale(started_db):
    db_path, general_tid = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    for i in range(3):
        db.add_message(general_tid, "user", f"real question {i}")

    result = run_cli(["get-stale-threads"], db_path)
    assert result.returncode == 0
    assert "A" in result.stdout


def test_get_messages_plain(started_db):
    db_path, general_tid = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    db.add_message(general_tid, "user", "hello world")
    db.add_message(general_tid, "assistant", "hi there")

    result = run_cli(["get-messages", "A", "--plain"], db_path)
    assert result.returncode == 0
    assert "user: hello world" in result.stdout
    assert "assistant: hi there" in result.stdout


# ------------------------------------------------------------------
# archive-thread CLI tests
# ------------------------------------------------------------------

def test_archive_thread_cli(started_db):
    """archive-thread sets status=archived and prints confirmation."""
    db_path, general_tid = started_db
    result = run_cli(["archive-thread", "A"], db_path)
    assert result.returncode == 0
    assert "Thread A archived" in result.stdout

    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    t = db.get_thread(general_tid)
    assert t is not None
    assert t["status"] == "archived"
    assert t["show_in_list"] == 0


# ------------------------------------------------------------------
# get-archive-candidates CLI tests
# ------------------------------------------------------------------

def test_get_archive_candidates_none(started_db):
    """Prints 'No archive candidates.' when nothing qualifies."""
    db_path, _ = started_db
    # Thread A is current — should be excluded
    result = run_cli(["get-archive-candidates"], db_path)
    assert result.returncode == 0
    assert "No archive candidates." in result.stdout


def test_get_archive_candidates_finds_done(started_db):
    """Lists a done non-current thread as a candidate."""
    db_path, _ = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    second_tid = db.create_thread("Second topic", session_id="")
    db.update_thread(second_tid, status="done")
    # Current is general_tid (label A), second_tid is done → candidate

    result = run_cli(["get-archive-candidates"], db_path)
    assert result.returncode == 0
    assert "[B]" in result.stdout
    assert "done" in result.stdout


def test_get_archive_candidates_excludes_archived(started_db):
    """Already-archived threads do not appear in candidate list."""
    db_path, _ = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    second_tid = db.create_thread("Second topic", session_id="")
    db.archive_thread(second_tid)

    result = run_cli(["get-archive-candidates"], db_path)
    assert result.returncode == 0
    assert "[B]" not in result.stdout


# ------------------------------------------------------------------
# unarchive-thread CLI tests
# ------------------------------------------------------------------

def test_unarchive_thread_cli(started_db):
    """unarchive-thread restores thread and prints 'Thread X unarchived.'"""
    db_path, _ = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))

    # Archive first, then unarchive via CLI
    second_tid = db.create_thread("Second topic", session_id="")
    db.archive_thread(second_tid)

    result = run_cli(["unarchive-thread", second_tid], db_path)
    assert result.returncode == 0
    assert "unarchived" in result.stdout

    t = db.get_thread(second_tid)
    assert t is not None
    assert t["status"] == "active"
    assert t["show_in_list"] == 1
    assert t["label"] is not None


def test_unarchive_thread_cli_by_uuid(started_db):
    """unarchive-thread accepts full UUID."""
    db_path, _ = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))

    tid = db.create_thread("Archived topic", session_id="")
    db.archive_thread(tid)

    result = run_cli(["unarchive-thread", tid], db_path)
    assert result.returncode == 0
    assert "unarchived" in result.stdout

    t = db.get_thread(tid)
    assert t is not None
    assert t["status"] == "active"
    assert t["show_in_list"] == 1


# ------------------------------------------------------------------
# show-topics filters archived threads
# ------------------------------------------------------------------

def test_show_topics_hides_archived(started_db):
    """show-topics does not display threads with show_in_list=0."""
    db_path, _ = started_db
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    second_tid = db.create_thread("Second topic", session_id="")
    db.archive_thread(second_tid)

    result = run_cli(["show-topics"], db_path)
    assert result.returncode == 0
    assert "[B]" not in result.stdout
    assert "[A]" in result.stdout


# ------------------------------------------------------------------
# _extract_decision_prompt tests
# ------------------------------------------------------------------

def test_extract_decision_prompt_with_question():
    """Extracts last sentence containing ? from assistant message."""
    sys.path.insert(0, SRC_DIR)
    from juggle_cli import _extract_decision_prompt
    result = _extract_decision_prompt(
        "We talked about things. Which option do you want?",
        "some user text"
    )
    assert result == "🤔 Which option do you want?"


def test_extract_decision_prompt_no_question_falls_back_to_user():
    """When no ? in assistant message, shows user message as 📬 prompt."""
    sys.path.insert(0, SRC_DIR)
    from juggle_cli import _extract_decision_prompt
    result = _extract_decision_prompt(
        "Implementation complete.",
        "Merge back to main locally, then push"
    )
    assert result == '📬 Respond to: "Merge back to main locally, then push"'


def test_extract_decision_prompt_truncates_long_question():
    """Questions longer than 80 chars are truncated."""
    sys.path.insert(0, SRC_DIR)
    from juggle_cli import _extract_decision_prompt
    long_q = "Do you want option A which does something or option B which does something else entirely?"
    result = _extract_decision_prompt(long_q, "user")
    assert result.startswith("🤔 ")
    assert len(result) <= 83  # "🤔 " + 80 chars


def test_extract_decision_prompt_truncates_long_user_message():
    """User messages longer than 60 chars are truncated with ..."""
    sys.path.insert(0, SRC_DIR)
    from juggle_cli import _extract_decision_prompt
    long_user = "This is a very long user message that goes on and on and says many things"
    result = _extract_decision_prompt(None, long_user)
    assert result.startswith('📬 Respond to: "')
    assert result.endswith('..."')


def test_extract_decision_prompt_no_messages():
    """Returns generic fallback when no messages."""
    sys.path.insert(0, SRC_DIR)
    from juggle_cli import _extract_decision_prompt
    result = _extract_decision_prompt(None, None)
    assert result == "🤔 Waiting for input"


# ------------------------------------------------------------------
# show-topics ⏸️ decision prompt rendering tests
# ------------------------------------------------------------------

def test_last_sentences_strips_code_block():
    msg = "Do the thing.\n```\n│\n├── [A] foo\n└── [B] bar\n```"
    result = _last_sentences(msg)
    assert "│" not in result
    assert "Do the thing" in result


def test_last_sentences_strips_tree_text_mixed_lines():
    """Lines starting with tree-drawing chars followed by text are stripped."""
    msg = "Here is a summary.\n├── ✅ [F] UUID storage + ephemeral label migration  done\n└── ❓ What next?\nPlease decide."
    result = _last_sentences(msg)
    assert "├──" not in result
    assert "└──" not in result
    assert "UUID storage" not in result
    # Non-tree text must survive
    assert "Please decide" in result or "Here is a summary" in result


def test_show_topics_waiting_thread_shows_decision_prompt(tmp_path, capsys):
    """⏸️ thread shows 🤔 decision prompt instead of Last: Q/A block."""
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    from juggle_cli import cmd_show_topics

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    db.set_active(True)
    tid_a = db.create_thread("Topic A", session_id="")
    db.set_current_thread(tid_a)
    tid_b = db.create_thread("Waiting Topic", session_id="")
    db.add_message(tid_b, "assistant", "Should I proceed with option A or B?")
    db.add_message(tid_b, "user", "Go with option A")

    # Patch get_db to return our test db
    import juggle_cli
    original_get_db = juggle_cli.get_db
    juggle_cli.get_db = lambda: db
    try:
        cmd_show_topics(None)
    finally:
        juggle_cli.get_db = original_get_db

    out = capsys.readouterr().out
    assert "🤔" in out or "📬" in out
    # The waiting thread section should not contain a Last: Q/A block
    waiting_section = out.split("Waiting Topic")[1].split("\n\n")[0]
    assert "Last:" not in waiting_section


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
    # First: spawn an idle agent
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        run_cli(["spawn-agent", "coder"], db_path)
    # Second: get-agent should reuse it
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
        result = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)

    agent_id = result.stdout.strip().split()[0]
    db = JuggleDB(str(db_path))
    agent = db.get_agent(agent_id)
    assert agent["status"] == "busy"
    assert agent["assigned_thread"] == thread_id


def test_release_agent_marks_idle(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]

    result = run_cli(["release-agent", agent_id], db_path)
    assert result.returncode == 0

    db = JuggleDB(str(db_path))
    agent = db.get_agent(agent_id)
    assert agent["status"] == "idle"
    assert agent["assigned_thread"] is None


def test_release_agent_adds_context_thread(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]
    run_cli(["release-agent", agent_id], db_path)

    db = JuggleDB(str(db_path))
    agent = db.get_agent(agent_id)
    context = json.loads(agent["context_threads"])
    assert thread_id in context


def test_release_agent_decommissions_pending(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
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
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["get-agent", thread_id, "--role", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]

    prompt_file = tmp_path / "task.txt"
    prompt_file.write_text("Do the thing.\npython3 juggle_cli.py complete-agent X result\n")

    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_SEND": "1"}):
        result = run_cli(["send-task", agent_id, str(prompt_file)], db_path)
    assert result.returncode == 0
    assert "sent" in result.stdout.lower()


def test_send_task_error_on_missing_prompt_file(started_db, tmp_path):
    db_path, thread_id = started_db
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%3"}):
        r = run_cli(["spawn-agent", "coder"], db_path)
    agent_id = r.stdout.strip().split()[0]

    result = run_cli(["send-task", agent_id, "/nonexistent/task.txt"], db_path)
    assert result.returncode == 1
    assert "not found" in result.stdout.lower()


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
    # Agent is busy (still assigned)
    result = run_cli(["archive-thread", thread_id], db_path)
    assert result.returncode == 0
    agent = JuggleDB(str(db_path)).get_agent(agent_id)
    assert agent["status"] == "decommission_pending"
