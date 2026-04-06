"""Tests for juggle_cli.py using subprocess."""
import json
import subprocess
import sys
from pathlib import Path

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
