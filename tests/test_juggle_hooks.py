"""Tests for juggle_hooks.py Stop handler and classification helpers."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB
from juggle_hooks import get_classification_candidates


@pytest.fixture
def active_db(tmp_path):
    # Use juggle.db so juggle_hooks.DB_PATH (_DATA_DIR / "juggle.db") resolves to the same file
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    db.set_active(True)
    tid = db.create_thread("Topic A", session_id="s1")
    db.set_current_thread(tid)
    return db


def test_stop_handler_captures_assistant_message(active_db, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active_db.db_path.parent))

    # Import after env is set so DB_PATH resolves correctly
    import importlib
    import juggle_hooks
    importlib.reload(juggle_hooks)

    data = {"last_assistant_message": "Here is my analysis of the auth flow."}

    with pytest.raises(SystemExit):
        juggle_hooks.handle_stop(data)

    messages = active_db.get_messages(active_db.get_current_thread(), token_budget=9999)
    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["content"] == "Here is my analysis of the auth flow."


def test_stop_handler_ignores_short_messages(active_db, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active_db.db_path.parent))
    import importlib
    import juggle_hooks
    importlib.reload(juggle_hooks)

    data = {"last_assistant_message": "ok"}  # too short

    with pytest.raises(SystemExit):
        juggle_hooks.handle_stop(data)

    messages = active_db.get_messages(active_db.get_current_thread(), token_budget=9999)
    assert not any(m["role"] == "assistant" for m in messages)


def test_stop_handler_missing_field_is_noop(active_db, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active_db.db_path.parent))
    import importlib
    import juggle_hooks
    importlib.reload(juggle_hooks)

    with pytest.raises(SystemExit):
        juggle_hooks.handle_stop({})  # no last_assistant_message key

    messages = active_db.get_messages(active_db.get_current_thread(), token_budget=9999)
    assert not any(m["role"] == "assistant" for m in messages)


# ---------------------------------------------------------------------------
# Classification candidate filter tests
# ---------------------------------------------------------------------------

def _make_thread(tid: str, status: str) -> dict:
    return {"thread_id": tid, "topic": f"Topic {tid}", "status": status}


def test_classification_candidates_excludes_done():
    threads = [
        _make_thread("A", "active"),
        _make_thread("B", "done"),
        _make_thread("C", "background"),
    ]
    candidates = get_classification_candidates(threads)
    ids = [t["thread_id"] for t in candidates]
    assert "B" not in ids
    assert "A" in ids
    assert "C" in ids


def test_classification_candidates_excludes_archived():
    threads = [
        _make_thread("A", "active"),
        _make_thread("B", "archived"),
    ]
    candidates = get_classification_candidates(threads)
    ids = [t["thread_id"] for t in candidates]
    assert "B" not in ids
    assert "A" in ids


def test_classification_candidates_all_closed_returns_empty():
    threads = [
        _make_thread("A", "done"),
        _make_thread("B", "archived"),
    ]
    candidates = get_classification_candidates(threads)
    assert candidates == []


def test_classification_candidates_includes_all_open_statuses():
    threads = [
        _make_thread("A", "active"),
        _make_thread("B", "background"),
        _make_thread("C", "idle"),
        _make_thread("D", "waiting"),
    ]
    candidates = get_classification_candidates(threads)
    assert len(candidates) == 4


def test_classification_candidates_empty_input():
    assert get_classification_candidates([]) == []
