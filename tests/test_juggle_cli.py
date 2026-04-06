"""Tests for juggle_cli.py using subprocess."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

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
    """Return a DB path after running `start` (no hook registration)."""
    db_path = tmp_path / "test.db"
    # Import directly to avoid settings.json modification
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    db.init_db()
    db.set_active(True)
    tid = db.create_thread("General", session_id="")
    db.set_current_thread(tid)
    return db_path


def test_init_db(db_path):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(db_path))
    db.init_db()
    assert db_path.exists()


def test_show_topics_empty(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    threads = db.get_all_threads()
    assert len(threads) == 1
    assert threads[0]["thread_id"] == "A"


def test_create_thread(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    tid = db.create_thread("New topic", session_id="")
    assert tid == "B"


def test_switch_thread(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    db.create_thread("Second topic", session_id="")
    db.set_current_thread("B")
    assert db.get_current_thread() == "B"


def test_update_thread_meta(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    db.update_thread("A", key_decisions=["Use SQLite"])
    t = db.get_thread("A")
    assert t is not None
    decisions = json.loads(t["key_decisions"])
    assert "Use SQLite" in decisions


def test_update_summary(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    db.update_thread("A", summary="We decided to use juggle.")
    t = db.get_thread("A")
    assert t is not None
    assert t["summary"] == "We decided to use juggle."


def test_close_thread(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    db.update_thread("A", status="closed")
    t = db.get_thread("A")
    assert t is not None
    assert t["status"] == "closed"


def test_add_shared(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    db.add_shared("decision", "Use JWT", source_thread="A")
    shared = db.get_shared_context()
    assert len(shared) == 1


def test_set_and_check_agent(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    db.update_thread("A", agent_task_id="task_abc", status="background")
    agents = db.get_background_agents()
    assert len(agents) == 1
    assert agents[0]["agent_task_id"] == "task_abc"


def test_complete_agent(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    db.update_thread("A", agent_task_id="task_abc", status="background")
    db.update_thread("A", agent_result="Done", status="done")
    db.add_notification("A", "Topic A complete")
    t = db.get_thread("A")
    assert t is not None
    assert t["status"] == "done"
    pending = db.get_pending_notifications()
    assert len(pending) == 1


def test_fail_agent(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    db.update_thread("A", status="failed", agent_result="timeout")
    t = db.get_thread("A")
    assert t is not None
    assert t["status"] == "failed"


def test_set_summarized_count(started_db):
    result = run_cli(["set-summarized-count", "A", "5"], started_db)
    assert result.returncode == 0
    assert "5" in result.stdout

    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    thread = db.get_thread("A")
    assert thread is not None
    assert thread["summarized_msg_count"] == 5


def test_get_stale_threads_empty(started_db):
    result = run_cli(["get-stale-threads"], started_db)
    assert result.returncode == 0
    assert "No stale" in result.stdout


def test_get_stale_threads_finds_stale(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    for i in range(3):
        db.add_message("A", "user", f"real question {i}")

    result = run_cli(["get-stale-threads"], started_db)
    assert result.returncode == 0
    assert "A" in result.stdout


def test_get_messages_plain(started_db):
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    db.add_message("A", "user", "hello world")
    db.add_message("A", "assistant", "hi there")

    result = run_cli(["get-messages", "A", "--plain"], started_db)
    assert result.returncode == 0
    assert "user: hello world" in result.stdout
    assert "assistant: hi there" in result.stdout


# ------------------------------------------------------------------
# archive-thread CLI tests
# ------------------------------------------------------------------

def test_archive_thread_cli(started_db):
    """archive-thread sets status=archived and prints confirmation."""
    result = run_cli(["archive-thread", "A"], started_db)
    assert result.returncode == 0
    assert "Thread A archived" in result.stdout

    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    t = db.get_thread("A")
    assert t is not None
    assert t["status"] == "archived"
    assert t["show_in_list"] == 0


# ------------------------------------------------------------------
# get-archive-candidates CLI tests
# ------------------------------------------------------------------

def test_get_archive_candidates_none(started_db):
    """Prints 'No archive candidates.' when nothing qualifies."""
    # Thread A is current — should be excluded
    result = run_cli(["get-archive-candidates"], started_db)
    assert result.returncode == 0
    assert "No archive candidates." in result.stdout


def test_get_archive_candidates_finds_done(started_db):
    """Lists a done non-current thread as a candidate."""
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    db.create_thread("Second topic", session_id="")
    db.update_thread("B", status="done")
    # Current is A, B is done → candidate

    result = run_cli(["get-archive-candidates"], started_db)
    assert result.returncode == 0
    assert "[B]" in result.stdout
    assert "done" in result.stdout


def test_get_archive_candidates_excludes_archived(started_db):
    """Already-archived threads do not appear in candidate list."""
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    db.create_thread("Second topic", session_id="")
    db.archive_thread("B")

    result = run_cli(["get-archive-candidates"], started_db)
    assert result.returncode == 0
    assert "[B]" not in result.stdout


# ------------------------------------------------------------------
# show-topics filters archived threads
# ------------------------------------------------------------------

def test_show_topics_hides_archived(started_db):
    """show-topics does not display threads with show_in_list=0."""
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB
    db = JuggleDB(str(started_db))
    db.create_thread("Second topic", session_id="")
    db.archive_thread("B")

    result = run_cli(["show-topics"], started_db)
    assert result.returncode == 0
    assert "[B]" not in result.stdout
    assert "[A]" in result.stdout
