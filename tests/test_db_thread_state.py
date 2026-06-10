"""JuggleDB tests: get_thread_state badge logic (split from test_juggle_db.py, 2026-06-10)."""

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
def test_get_thread_state_current(db):
    """get_thread_state returns 👉 for the current thread."""
    tid = db.create_thread("Topic A", session_id="s1")
    thread = db.get_thread(tid)
    state = get_thread_state(db, thread, current_thread_id=tid)
    assert state == "👉"


def test_get_thread_state_background(db):
    """get_thread_state returns 🏃 for background threads."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.update_thread(tid, status="background", agent_task_id="task_123")
    thread = db.get_thread(tid)
    state = get_thread_state(db, thread, current_thread_id="not-this-thread")
    assert state == "🏃\u200d♂️"


def test_get_thread_state_done(db):
    """get_thread_state returns ✅ for done threads."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.update_thread(tid, status="done")
    thread = db.get_thread(tid)
    state = get_thread_state(db, thread, current_thread_id="not-this-thread")
    assert state == "✅"


def test_get_thread_state_failed(db):
    """get_thread_state returns ❌ for failed threads."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.update_thread(tid, status="failed")
    thread = db.get_thread(tid)
    state = get_thread_state(db, thread, current_thread_id="not-this-thread")
    assert state == "❌"


def test_get_thread_state_archived(db):
    """get_thread_state returns 🗄️ for threads inactive > 48 hours."""
    from datetime import datetime, timezone, timedelta

    tid = db.create_thread("Topic A", session_id="s1")
    old_time = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    db.update_thread(tid, last_active=old_time)
    thread = db.get_thread(tid)
    state = get_thread_state(db, thread, current_thread_id="not-this-thread")
    assert state == "🗄️"


def test_get_thread_state_waiting(db):
    """get_thread_state returns ⏸️ when last assistant message ends with ?."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "some question")
    db.add_message(tid, "assistant", "Do you want the Secure flag set on the cookie?")
    thread = db.get_thread(tid)
    state = get_thread_state(db, thread, current_thread_id="not-this-thread")
    assert state == "⏸️"


def test_get_thread_state_idle(db):
    """get_thread_state returns 💤 when last assistant message has no ? and inactive > 30 min."""
    from datetime import datetime, timezone, timedelta

    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "some question")
    db.add_message(tid, "assistant", "Here is the answer.")
    old_time = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
    db.update_thread(tid, last_active=old_time)
    thread = db.get_thread(tid)
    state = get_thread_state(db, thread, current_thread_id="not-this-thread")
    assert state == "💤"


def test_get_thread_state_no_badge_recent_active(db):
    """get_thread_state returns empty string for recently active threads with no question."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "some question")
    db.add_message(tid, "assistant", "Here is the answer.")
    # last_active is just now (set by add_message), so not idle
    thread = db.get_thread(tid)
    state = get_thread_state(db, thread, current_thread_id="not-this-thread")
    assert state == ""


def test_get_thread_state_priority_current_over_background(db):
    """current state wins over background."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.update_thread(tid, status="background", agent_task_id="task_1")
    thread = db.get_thread(tid)
    state = get_thread_state(db, thread, current_thread_id=tid)
    assert state == "👉"


def test_get_thread_state_done_unanswered_question_returns_paused(db):
    """get_thread_state returns ⏸️ for done threads where last assistant msg ends with ? and no user reply."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "some question")
    db.add_message(tid, "assistant", "Do you want me to proceed?")
    db.update_thread(tid, status="done")
    thread = db.get_thread(tid)
    state = get_thread_state(db, thread, current_thread_id="not-this-thread")
    assert state == "⏸️"


def test_get_thread_state_done_answered_question_returns_done(db):
    """get_thread_state returns ✅ for done threads where user replied after assistant question."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "some question")
    db.add_message(tid, "assistant", "Do you want me to proceed?")
    db.add_message(tid, "user", "yes please")
    db.update_thread(tid, status="done")
    thread = db.get_thread(tid)
    state = get_thread_state(db, thread, current_thread_id="not-this-thread")
    assert state == "✅"


def test_get_thread_state_done_junk_only_reply_returns_paused(db):
    """get_thread_state returns ⏸️ for done threads where only junk messages follow the question."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "some question")
    db.add_message(tid, "assistant", "Do you want me to proceed?")
    # Only junk "replies" — slash commands and task-notifications
    db.add_message(tid, "user", "/juggle:show-topics")
    db.add_message(tid, "user", "<task-notification>task-id: abc</task-notification>")
    db.update_thread(tid, status="done")
    thread = db.get_thread(tid)
    state = get_thread_state(db, thread, current_thread_id="not-this-thread")
    assert state == "⏸️"


def test_get_last_exchange_skips_junk_user_messages(db):
    """get_last_exchange should skip junk user messages and fall back to the previous real one."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "real question about auth")
    db.add_message(tid, "assistant", "Here is the answer")
    # Add junk user messages after the real exchange
    db.add_message(
        tid,
        "user",
        '"><tool_uses>2</tool_uses><duration_ms>5292</duration_ms></usage></task-notification>',
    )
    db.add_message(
        tid, "user", "<task-notification>some task-id content</task-notification>"
    )
    db.add_message(tid, "user", "/juggle:show-topics")

    result = db.get_last_exchange(tid)
    assert result["last_user"] == "real question about auth"
    assert result["last_assistant"] == "Here is the answer"

