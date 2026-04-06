"""Tests for JuggleDB."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


def test_is_active_default_false(db):
    assert db.is_active() is False


def test_set_active(db):
    db.set_active(True)
    assert db.is_active() is True
    db.set_active(False)
    assert db.is_active() is False


def test_set_active_sets_started_at_once(db):
    db.set_active(True)
    with db._connect() as conn:
        row1 = conn.execute("SELECT value FROM session WHERE key='started_at'").fetchone()
    db.set_active(True)
    with db._connect() as conn:
        row2 = conn.execute("SELECT value FROM session WHERE key='started_at'").fetchone()
    assert row1["value"] == row2["value"]  # not overwritten on second call


def test_create_thread_returns_a(db):
    tid = db.create_thread("My topic", session_id="s1")
    assert tid == "A"


def test_create_thread_sequential(db):
    a = db.create_thread("Topic A", session_id="s1")
    b = db.create_thread("Topic B", session_id="s1")
    assert a == "A"
    assert b == "B"


def test_create_thread_max_4(db):
    for i, label in enumerate(["A", "B", "C", "D"]):
        db.create_thread(f"Topic {label}", session_id="s1")
    with pytest.raises(ValueError, match="Maximum of 4"):
        db.create_thread("Topic E", session_id="s1")


def test_get_thread(db):
    db.create_thread("My topic", session_id="s1")
    t = db.get_thread("A")
    assert t is not None
    assert t["topic"] == "My topic"
    assert t["status"] == "active"


def test_get_thread_missing(db):
    assert db.get_thread("Z") is None


def test_get_all_threads(db):
    db.create_thread("A topic", session_id="s1")
    db.create_thread("B topic", session_id="s1")
    threads = db.get_all_threads()
    assert len(threads) == 2
    assert threads[0]["thread_id"] == "A"
    assert threads[1]["thread_id"] == "B"


def test_update_thread(db):
    db.create_thread("My topic", session_id="s1")
    db.update_thread("A", summary="Updated summary", status="background")
    t = db.get_thread("A")
    assert t["summary"] == "Updated summary"
    assert t["status"] == "background"


def test_update_thread_list_serialized(db):
    db.create_thread("My topic", session_id="s1")
    db.update_thread("A", key_decisions=["decision 1", "decision 2"])
    t = db.get_thread("A")
    # Should be stored as JSON string
    parsed = json.loads(t["key_decisions"])
    assert parsed == ["decision 1", "decision 2"]


def test_set_get_current_thread(db):
    db.create_thread("My topic", session_id="s1")
    assert db.get_current_thread() is None
    db.set_current_thread("A")
    assert db.get_current_thread() == "A"


def test_add_and_get_messages(db):
    db.create_thread("My topic", session_id="s1")
    db.add_message("A", "user", "Hello world")
    db.add_message("A", "assistant", "Hi there")
    msgs = db.get_messages("A", token_budget=1500)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


def test_get_messages_token_budget(db):
    db.create_thread("My topic", session_id="s1")
    # Add many messages; budget should limit what's returned
    for i in range(20):
        db.add_message("A", "user", "x" * 400)  # ~100 tokens each
    msgs = db.get_messages("A", token_budget=500)
    # 500 token budget / ~100 tokens per msg = ~5 msgs
    assert len(msgs) <= 6
    assert len(msgs) > 0
    # Should be in chronological order (last messages)
    assert all(m["role"] == "user" for m in msgs)


def test_get_message_count(db):
    db.create_thread("My topic", session_id="s1")
    assert db.get_message_count("A") == 0
    db.add_message("A", "user", "hello")
    assert db.get_message_count("A") == 1


def test_add_shared(db):
    db.create_thread("My topic", session_id="s1")
    db.add_shared("decision", "Use JWT", source_thread="A")
    shared = db.get_shared_context()
    assert len(shared) == 1
    assert shared[0]["context_type"] == "decision"
    assert shared[0]["content"] == "Use JWT"
    assert shared[0]["source_thread"] == "A"


def test_add_shared_no_source(db):
    db.add_shared("fact", "Python 3.11")
    shared = db.get_shared_context()
    assert shared[0]["source_thread"] is None


def test_notifications(db):
    db.create_thread("My topic", session_id="s1")
    db.add_notification("A", "Agent done")
    pending = db.get_pending_notifications()
    assert len(pending) == 1
    assert pending[0]["message"] == "Agent done"

    db.mark_notifications_delivered([pending[0]["id"]])
    assert db.get_pending_notifications() == []


def test_mark_notifications_empty_list(db):
    # Should not raise
    db.mark_notifications_delivered([])


def test_get_background_agents(db):
    db.create_thread("My topic", session_id="s1")
    assert db.get_background_agents() == []

    db.update_thread("A", status="background", agent_task_id="task_123")
    agents = db.get_background_agents()
    assert len(agents) == 1
    assert agents[0]["agent_task_id"] == "task_123"


def test_summarized_msg_count_default_zero(db):
    tid = db.create_thread("Topic A", session_id="s1")
    thread = db.get_thread(tid)
    assert thread["summarized_msg_count"] == 0


def test_set_summarized_count(db):
    tid = db.create_thread("Topic A", session_id="s1")
    db.set_summarized_count(tid, 5)
    thread = db.get_thread(tid)
    assert thread["summarized_msg_count"] == 5


def test_get_message_count_excludes_junk(db):
    db.set_active(True)
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "real question about auth")
    db.add_message(tid, "user", "/juggle:show-topics")           # junk: slash cmd
    db.add_message(tid, "user", "<task-notification>...</task-notification>")  # junk
    db.add_message(tid, "user", "another real question")
    db.add_message(tid, "assistant", "some response")             # excluded: not user
    assert db.get_message_count(tid, exclude_junk=True) == 2
    assert db.get_message_count(tid, exclude_junk=False) == 4    # counts all user rows


def test_get_stale_threads(db):
    db.set_active(True)
    tid = db.create_thread("Topic A", session_id="s1")
    for i in range(3):
        db.add_message(tid, "user", f"real question {i}")
    stale = db.get_stale_threads(threshold=3)
    assert len(stale) == 1
    assert stale[0]["thread_id"] == tid
    assert stale[0]["delta"] == 3


def test_get_stale_threads_not_stale_after_set(db):
    db.set_active(True)
    tid = db.create_thread("Topic A", session_id="s1")
    for i in range(3):
        db.add_message(tid, "user", f"real question {i}")
    db.set_summarized_count(tid, 3)
    stale = db.get_stale_threads(threshold=3)
    assert len(stale) == 0
