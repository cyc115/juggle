import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from unittest.mock import Mock, patch


@pytest.fixture
def mock_db():
    db = Mock()
    db.init_db = Mock()
    db.set_active = Mock()
    db.get_all_threads = Mock(return_value=[])
    db.create_thread = Mock(return_value="thread-uuid-1")
    db.set_current_thread = Mock()
    db.get_thread = Mock(return_value={"id": "thread-uuid-1", "label": "General"})
    return db


@pytest.fixture
def mock_get_db(mock_db):
    with patch("juggle_cmd_threads.get_db", return_value=mock_db):
        yield mock_db


def test_cmd_start_creates_db_and_watchdog(mock_get_db, tmp_path):
    from juggle_cmd_threads import cmd_start

    with patch("juggle_cmd_threads._DATA_DIR", tmp_path):
        with patch("juggle_cmd_threads._start_watchdog_for_cmd_start"):
            with patch("juggle_cmd_threads._maybe_start_talkback"):
                with patch("juggle_cmd_threads._get_version", return_value="1.0"):
                    with patch("builtins.print") as mock_print:
                        cmd_start(None)

    mock_get_db.init_db.assert_called_once()
    mock_get_db.set_active.assert_called_once_with(True)
    assert mock_print.called


def test_cmd_start_uses_existing_thread(mock_get_db):
    from juggle_cmd_threads import cmd_start

    existing_thread = {
        "id": "existing-uuid",
        "label": "Existing",
        "status": "active",
        "last_active": "2026-05-18T10:00:00Z",
    }
    mock_get_db.get_all_threads.return_value = [existing_thread]
    mock_get_db.get_thread.return_value = existing_thread

    with patch("juggle_cmd_threads._DATA_DIR"):
        with patch("juggle_cmd_threads._start_watchdog_for_cmd_start"):
            with patch("juggle_cmd_threads._maybe_start_talkback"):
                with patch("juggle_cmd_threads._get_version", return_value="1.0"):
                    with patch(
                        "juggle_context.build_startup_output", return_value="startup"
                    ):
                        with patch("builtins.print"):
                            cmd_start(None)

    mock_get_db.set_current_thread.assert_called_with("existing-uuid")


def test_cmd_stop_sets_inactive(mock_get_db):
    from juggle_cmd_threads import cmd_stop

    mock_get_db.get_all_threads.return_value = []

    with patch("juggle_watchdog_singleton.stop_watchdog"):
        with patch("builtins.print"):
            cmd_stop(None)

    mock_get_db.set_active.assert_called_once_with(False)


def test_cmd_stop_prints_threads(mock_get_db):
    from juggle_cmd_threads import cmd_stop

    threads = [
        {"id": "t1", "label": "A", "title": "topic1", "state": "open"},
        {"id": "t2", "user_label": "B", "title": "topic2", "state": "done"},
    ]
    mock_get_db.get_all_threads.return_value = threads

    with patch("juggle_watchdog_singleton.stop_watchdog"):
        with patch("builtins.print") as mock_print:
            cmd_stop(None)

    assert mock_print.call_count >= 3


def test_cmd_create_thread_creates_and_sets_current(mock_get_db):
    from juggle_cmd_threads import cmd_create_thread
    import argparse

    args = argparse.Namespace(topic="New Topic")

    with patch("juggle_cmd_threads._generate_title_for_thread"):
        with patch("juggle_cmd_threads.threading.Thread"):
            with patch("builtins.print"):
                cmd_create_thread(args)

    mock_get_db.create_thread.assert_called_once_with("New Topic", session_id="")
    mock_get_db.set_current_thread.assert_called_with("thread-uuid-1")



def test_cmd_create_thread_prints_output(mock_get_db):
    from juggle_cmd_threads import cmd_create_thread
    import argparse

    args = argparse.Namespace(topic="Test")
    mock_get_db.get_thread.return_value = {"id": "uuid", "label": "Test Label"}

    with patch("juggle_cmd_threads._generate_title_for_thread"):
        with patch("juggle_cmd_threads.threading.Thread"):
            with patch("builtins.print") as mock_print:
                cmd_create_thread(args)

    mock_print.assert_called_once()
    assert "Created Topic" in str(mock_print.call_args)


def test_render_briefing_uses_node_state_vocab(tmp_path):
    """Regression (2026-06-29, P8 Task 4.2 conversation reads-collapse):
    _render_briefing's header def was migrated status->state but the NEXT-STEPS
    branches still read an UNDEFINED `status`, and `summary` was referenced but
    never defined — so rendering a briefing for any thread WITH messages crashed
    with NameError. _render_briefing is otherwise untested, so the green suite
    missed it. Pin the node-vocab branches + the summary fallback past the
    empty-thread guard."""
    from juggle_db import JuggleDB
    from juggle_cmd_threads import _render_briefing

    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()

    # background state -> "Agent is running" next step (reads node state vocab)
    tid = db.create_thread("brief test", session_id="s1")
    db.update_thread(tid, status="background")
    db.add_message(tid, "assistant", "did the thing")
    out = _render_briefing(db.get_thread(tid), db)
    assert "Agent is running" in out

    # failed -> node state 'failed-exec' -> review-what-failed branch
    db.update_thread(tid, status="failed")
    out = _render_briefing(db.get_thread(tid), db)
    assert "Review what failed" in out

    # summary fallback: a user-only thread with a node summary renders it
    tid2 = db.create_thread("brief test 2", session_id="s1")
    db.add_message(tid2, "user", "hello")
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET summary=? WHERE id=? AND kind='conversation'",
            ("my summary text", tid2),
        )
        conn.commit()
    out2 = _render_briefing(db.get_thread(tid2), db)
    assert "my summary text" in out2
