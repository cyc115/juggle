"""JuggleDB tests: messages, token budget, summarized counts, stale threads, recent exchanges (split from test_juggle_db.py, 2026-06-10)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_context import get_thread_state
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d
def test_add_and_get_messages(db):
    tid = db.create_thread("My topic", session_id="s1")
    db.add_message(tid, "user", "Hello world")
    db.add_message(tid, "assistant", "Hi there")
    msgs = db.get_messages(tid, token_budget=1500)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


def test_get_messages_token_budget(db):
    tid = db.create_thread("My topic", session_id="s1")
    # Add many messages; budget should limit what's returned
    for _ in range(20):
        db.add_message(tid, "user", "x" * 400)  # ~100 tokens each
    msgs = db.get_messages(tid, token_budget=500)
    # 500 token budget / ~100 tokens per msg = ~5 msgs
    assert len(msgs) <= 6
    assert len(msgs) > 0
    # Should be in chronological order (last messages)
    assert all(m["role"] == "user" for m in msgs)


def test_get_message_count(db):
    tid = db.create_thread("My topic", session_id="s1")
    assert db.get_message_count(tid) == 0
    db.add_message(tid, "user", "hello")
    assert db.get_message_count(tid) == 1


def test_summarized_msg_count_default_zero(db):
    tid = db.create_thread("Topic A", session_id="s1")
    thread = db.get_thread(tid)
    assert thread["summarized_msg_count"] == 0


def test_set_summarized_count(db):
    tid = db.create_thread("Topic A", session_id="s1")
    db.update_thread(tid, summarized_msg_count=5)
    thread = db.get_thread(tid)
    assert thread["summarized_msg_count"] == 5


def test_get_message_count_excludes_junk(db):
    db.set_active(True)
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "real question about auth")
    db.add_message(tid, "user", "/juggle:show-topics")  # junk: slash cmd
    db.add_message(tid, "user", "<task-notification>...</task-notification>")  # junk
    db.add_message(tid, "user", "another real question")
    db.add_message(tid, "assistant", "some response")  # excluded: not user
    assert db.get_message_count(tid, exclude_junk=True) == 2
    assert db.get_message_count(tid, exclude_junk=False) == 4  # counts all user rows


def test_get_stale_threads(db):
    db.set_active(True)
    tid = db.create_thread("Topic A", session_id="s1")
    for i in range(3):
        db.add_message(tid, "user", f"real question {i}")
    stale = db.get_stale_threads(threshold=3)
    assert len(stale) == 1
    assert stale[0]["id"] == tid
    assert stale[0]["delta"] == 3


def test_get_stale_threads_not_stale_after_set(db):
    db.set_active(True)
    tid = db.create_thread("Topic A", session_id="s1")
    for i in range(3):
        db.add_message(tid, "user", f"real question {i}")
    db.update_thread(tid, summarized_msg_count=3)
    stale = db.get_stale_threads(threshold=3)
    assert len(stale) == 0


def test_get_recent_exchanges_basic(db):
    """get_recent_exchanges returns last n Q/A pairs, most recent first."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "first question")
    db.add_message(tid, "assistant", "first answer")
    db.add_message(tid, "user", "second question")
    db.add_message(tid, "assistant", "second answer")

    exchanges = db.get_recent_exchanges(tid, n=2)
    assert len(exchanges) == 2
    # most recent first
    assert exchanges[0]["user"] == "second question"
    assert exchanges[0]["assistant"] == "second answer"
    assert exchanges[1]["user"] == "first question"
    assert exchanges[1]["assistant"] == "first answer"


def test_get_recent_exchanges_skips_junk(db):
    """get_recent_exchanges skips junk user messages."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "real question")
    db.add_message(tid, "assistant", "real answer")
    db.add_message(tid, "user", "/juggle:show-topics")  # junk
    db.add_message(
        tid, "user", "<task-notification>task-id</task-notification>"
    )  # junk

    exchanges = db.get_recent_exchanges(tid, n=2)
    assert len(exchanges) == 1
    assert exchanges[0]["user"] == "real question"
    assert exchanges[0]["assistant"] == "real answer"


def test_get_recent_exchanges_no_assistant_yet(db):
    """get_recent_exchanges returns None assistant when none exists yet."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "my question")

    exchanges = db.get_recent_exchanges(tid, n=2)
    assert len(exchanges) == 1
    assert exchanges[0]["user"] == "my question"
    assert exchanges[0]["assistant"] is None


def test_get_recent_exchanges_empty(db):
    """get_recent_exchanges returns empty list when no messages."""
    tid = db.create_thread("Topic A", session_id="s1")
    exchanges = db.get_recent_exchanges(tid, n=2)
    assert exchanges == []


def test_get_recent_exchanges_n_limits_results(db):
    """get_recent_exchanges respects n parameter."""
    tid = db.create_thread("Topic A", session_id="s1")
    for i in range(5):
        db.add_message(tid, "user", f"question {i}")
        db.add_message(tid, "assistant", f"answer {i}")

    exchanges = db.get_recent_exchanges(tid, n=2)
    assert len(exchanges) == 2
    # most recent first
    assert exchanges[0]["user"] == "question 4"
    assert exchanges[1]["user"] == "question 3"


