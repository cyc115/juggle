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
        with patch("juggle_cmd_threads._start_watchdog"):
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
        with patch("juggle_cmd_threads._start_watchdog"):
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

    with patch("juggle_cmd_threads._stop_watchdog"):
        with patch("builtins.print"):
            cmd_stop(None)

    mock_get_db.set_active.assert_called_once_with(False)


def test_cmd_stop_prints_threads(mock_get_db):
    from juggle_cmd_threads import cmd_stop

    threads = [
        {"id": "t1", "label": "A", "topic": "topic1", "status": "active"},
        {"id": "t2", "user_label": "B", "topic": "topic2", "status": "done"},
    ]
    mock_get_db.get_all_threads.return_value = threads

    with patch("juggle_cmd_threads._stop_watchdog"):
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


def test_cmd_create_thread_launches_background_threads(mock_get_db):
    from juggle_cmd_threads import cmd_create_thread
    import argparse

    args = argparse.Namespace(topic="New Topic")

    with patch("juggle_cmd_threads._generate_title_for_thread"):
        with patch("juggle_cmd_threads.threading.Thread") as mock_thread_cls:
            with patch("builtins.print"):
                cmd_create_thread(args)

    assert mock_thread_cls.call_count >= 2


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


# ---------------------------------------------------------------------------
# reflect() gate regression tests
# ---------------------------------------------------------------------------


def _make_thread(show_in_list=1, summarized_msg_count=0, last_reflect_msg_count=0):
    return {
        "id": "thread-uuid-1",
        "label": "General",
        "user_label": None,
        "show_in_list": show_in_list,
        "summarized_msg_count": summarized_msg_count,
        "last_reflect_msg_count": last_reflect_msg_count,
        "memory_loaded": 0,
    }


def test_reflect_skipped_when_internal_thread(mock_get_db):
    """Gate 1: show_in_list=0 → reflect() never called."""
    from juggle_cmd_threads import cmd_create_thread
    import argparse

    mock_client = Mock()
    mock_client.reflect.return_value = "some memory"
    mock_get_db.get_thread.return_value = _make_thread(show_in_list=0)

    args = argparse.Namespace(topic="agent task xyz")
    with patch("juggle_cmd_threads._generate_title_for_thread"):
        with patch("juggle_cli_common._get_hindsight_client", return_value=mock_client):
            with patch("builtins.print"):
                cmd_create_thread(args)

    mock_client.reflect.assert_not_called()
    mock_get_db.update_thread.assert_called_with("thread-uuid-1", memory_loaded=1)


def test_reflect_fires_for_user_thread_first_call(mock_get_db):
    """Gate 2: first call (last_reflect_msg_count=0) → reflect() fires."""
    from juggle_cmd_threads import cmd_create_thread
    import argparse

    mock_client = Mock()
    mock_client.reflect.return_value = "personal context"
    mock_get_db.get_thread.return_value = _make_thread(
        show_in_list=1, summarized_msg_count=0, last_reflect_msg_count=0
    )

    args = argparse.Namespace(topic="My user topic")
    with patch("juggle_cmd_threads._generate_title_for_thread"):
        with patch("juggle_cli_common._get_hindsight_client", return_value=mock_client):
            with patch("builtins.print"):
                cmd_create_thread(args)

    mock_client.reflect.assert_called_once_with("My user topic")
    mock_get_db.update_thread.assert_called_with(
        "thread-uuid-1",
        memory_context="personal context",
        memory_loaded=1,
        last_reflect_msg_count=0,
    )


def test_reflect_skipped_when_msg_delta_below_threshold(mock_get_db):
    """Gate 2: delta=2 < MIN_REFLECT_MSG_DELTA=5 → reflect() skipped."""
    from juggle_cmd_threads import cmd_create_thread
    import argparse

    mock_client = Mock()
    mock_client.reflect.return_value = "memory"
    mock_get_db.get_thread.return_value = _make_thread(
        show_in_list=1, summarized_msg_count=12, last_reflect_msg_count=10
    )

    args = argparse.Namespace(topic="My topic")
    with patch("juggle_cmd_threads._generate_title_for_thread"):
        with patch("juggle_cli_common._get_hindsight_client", return_value=mock_client):
            with patch("builtins.print"):
                cmd_create_thread(args)

    mock_client.reflect.assert_not_called()


def test_reflect_fires_when_msg_delta_meets_threshold(mock_get_db):
    """Gate 2: delta=5 >= MIN_REFLECT_MSG_DELTA → reflect() fires and updates count."""
    from juggle_cmd_threads import cmd_create_thread
    import argparse

    mock_client = Mock()
    mock_client.reflect.return_value = "fresh context"
    mock_get_db.get_thread.return_value = _make_thread(
        show_in_list=1, summarized_msg_count=15, last_reflect_msg_count=10
    )

    args = argparse.Namespace(topic="My evolving topic")
    with patch("juggle_cmd_threads._generate_title_for_thread"):
        with patch("juggle_cli_common._get_hindsight_client", return_value=mock_client):
            with patch("builtins.print"):
                cmd_create_thread(args)

    mock_client.reflect.assert_called_once_with("My evolving topic")
    mock_get_db.update_thread.assert_called_with(
        "thread-uuid-1",
        memory_context="fresh context",
        memory_loaded=1,
        last_reflect_msg_count=15,
    )
