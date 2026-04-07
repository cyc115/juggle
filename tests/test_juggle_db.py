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
    """create_thread returns a UUID (not a letter)."""
    import re
    tid = db.create_thread("My topic", session_id="s1")
    assert re.match(r"^[0-9a-f-]{36}$", tid), f"Expected UUID, got: {tid}"


def test_create_thread_sequential(db):
    """Sequential threads get sequential labels A, B."""
    a = db.create_thread("Topic A", session_id="s1")
    b = db.create_thread("Topic B", session_id="s1")
    assert db.get_thread(a)["label"] == "A"
    assert db.get_thread(b)["label"] == "B"


def test_create_thread_max_10(db):
    from juggle_db import MAX_THREADS
    for i in range(MAX_THREADS):
        db.create_thread(f"Topic {i}", session_id="s1")
    with pytest.raises(ValueError, match=f"Maximum of {MAX_THREADS}"):
        db.create_thread("Topic overflow", session_id="s1")


def test_get_thread(db):
    tid = db.create_thread("My topic", session_id="s1")
    t = db.get_thread(tid)
    assert t is not None
    assert t["topic"] == "My topic"
    assert t["status"] == "active"


def test_get_thread_missing(db):
    assert db.get_thread("not-a-real-uuid") is None


def test_get_all_threads(db):
    ta = db.create_thread("A topic", session_id="s1")
    tb = db.create_thread("B topic", session_id="s1")
    threads = db.get_all_threads()
    assert len(threads) == 2
    assert threads[0]["id"] == ta
    assert threads[1]["id"] == tb


def test_update_thread(db):
    tid = db.create_thread("My topic", session_id="s1")
    db.update_thread(tid, summary="Updated summary", status="background")
    t = db.get_thread(tid)
    assert t["summary"] == "Updated summary"
    assert t["status"] == "background"


def test_update_thread_list_serialized(db):
    tid = db.create_thread("My topic", session_id="s1")
    db.update_thread(tid, key_decisions=["decision 1", "decision 2"])
    t = db.get_thread(tid)
    # Should be stored as JSON string
    parsed = json.loads(t["key_decisions"])
    assert parsed == ["decision 1", "decision 2"]


def test_set_get_current_thread(db):
    db.create_thread("My topic", session_id="s1")
    assert db.get_current_thread() is None
    db.set_current_thread("A")
    assert db.get_current_thread() == "A"


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
    tid = db.create_thread("My topic", session_id="s1")
    assert db.get_background_agents() == []

    db.update_thread(tid, status="background", agent_task_id="task_123")
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
    assert stale[0]["id"] == tid
    assert stale[0]["delta"] == 3


def test_get_stale_threads_not_stale_after_set(db):
    db.set_active(True)
    tid = db.create_thread("Topic A", session_id="s1")
    for i in range(3):
        db.add_message(tid, "user", f"real question {i}")
    db.set_summarized_count(tid, 3)
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
    db.add_message(tid, "user", "/juggle:show-topics")          # junk
    db.add_message(tid, "user", "<task-notification>task-id</task-notification>")  # junk

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


def test_get_thread_state_current(db):
    """get_thread_state returns 👉 for the current thread."""
    tid = db.create_thread("Topic A", session_id="s1")
    thread = db.get_thread(tid)
    state = db.get_thread_state(thread, current_thread_id=tid)
    assert state == "👉"


def test_get_thread_state_background(db):
    """get_thread_state returns 🏃 for background threads."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.update_thread(tid, status="background", agent_task_id="task_123")
    thread = db.get_thread(tid)
    state = db.get_thread_state(thread, current_thread_id="not-this-thread")
    assert state == "🏃\u200d♂️"


def test_get_thread_state_done(db):
    """get_thread_state returns ✅ for done threads."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.update_thread(tid, status="done")
    thread = db.get_thread(tid)
    state = db.get_thread_state(thread, current_thread_id="not-this-thread")
    assert state == "✅"


def test_get_thread_state_failed(db):
    """get_thread_state returns ❌ for failed threads."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.update_thread(tid, status="failed")
    thread = db.get_thread(tid)
    state = db.get_thread_state(thread, current_thread_id="not-this-thread")
    assert state == "❌"


def test_get_thread_state_archived(db):
    """get_thread_state returns 🗄️ for threads inactive > 48 hours."""
    from datetime import datetime, timezone, timedelta
    tid = db.create_thread("Topic A", session_id="s1")
    old_time = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    db.update_thread(tid, last_active=old_time)
    thread = db.get_thread(tid)
    state = db.get_thread_state(thread, current_thread_id="not-this-thread")
    assert state == "🗄️"


def test_get_thread_state_waiting(db):
    """get_thread_state returns ⏸️ when last assistant message ends with ?."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "some question")
    db.add_message(tid, "assistant", "Do you want the Secure flag set on the cookie?")
    thread = db.get_thread(tid)
    state = db.get_thread_state(thread, current_thread_id="not-this-thread")
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
    state = db.get_thread_state(thread, current_thread_id="not-this-thread")
    assert state == "💤"


def test_get_thread_state_no_badge_recent_active(db):
    """get_thread_state returns empty string for recently active threads with no question."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "some question")
    db.add_message(tid, "assistant", "Here is the answer.")
    # last_active is just now (set by add_message), so not idle
    thread = db.get_thread(tid)
    state = db.get_thread_state(thread, current_thread_id="not-this-thread")
    assert state == ""


def test_get_thread_state_priority_current_over_background(db):
    """current state wins over background."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.update_thread(tid, status="background", agent_task_id="task_1")
    thread = db.get_thread(tid)
    state = db.get_thread_state(thread, current_thread_id=tid)
    assert state == "👉"


def test_get_thread_state_done_unanswered_question_returns_paused(db):
    """get_thread_state returns ⏸️ for done threads where last assistant msg ends with ? and no user reply."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "some question")
    db.add_message(tid, "assistant", "Do you want me to proceed?")
    db.update_thread(tid, status="done")
    thread = db.get_thread(tid)
    state = db.get_thread_state(thread, current_thread_id="not-this-thread")
    assert state == "⏸️"


def test_get_thread_state_done_answered_question_returns_done(db):
    """get_thread_state returns ✅ for done threads where user replied after assistant question."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "some question")
    db.add_message(tid, "assistant", "Do you want me to proceed?")
    db.add_message(tid, "user", "yes please")
    db.update_thread(tid, status="done")
    thread = db.get_thread(tid)
    state = db.get_thread_state(thread, current_thread_id="not-this-thread")
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
    state = db.get_thread_state(thread, current_thread_id="not-this-thread")
    assert state == "⏸️"


def test_get_last_exchange_skips_junk_user_messages(db):
    """get_last_exchange should skip junk user messages and fall back to the previous real one."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.add_message(tid, "user", "real question about auth")
    db.add_message(tid, "assistant", "Here is the answer")
    # Add junk user messages after the real exchange
    db.add_message(tid, "user", '"><tool_uses>2</tool_uses><duration_ms>5292</duration_ms></usage></task-notification>')
    db.add_message(tid, "user", "<task-notification>some task-id content</task-notification>")
    db.add_message(tid, "user", "/juggle:show-topics")

    result = db.get_last_exchange(tid)
    assert result["last_user"] == "real question about auth"
    assert result["last_assistant"] == "Here is the answer"


# ------------------------------------------------------------------
# archive_thread tests
# ------------------------------------------------------------------

def test_archive_thread_sets_status_and_show_in_list(db):
    """archive_thread sets status='archived' and show_in_list=0."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.archive_thread(tid)
    t = db.get_thread(tid)
    assert t is not None
    assert t["status"] == "archived"
    assert t["show_in_list"] == 0


def test_archive_thread_does_not_delete(db):
    """archive_thread does not delete the thread row."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.archive_thread(tid)
    assert db.get_thread(tid) is not None


def test_show_in_list_defaults_to_1(db):
    """New threads have show_in_list=1 by default."""
    tid = db.create_thread("Topic A", session_id="s1")
    t = db.get_thread(tid)
    assert t is not None
    assert t["show_in_list"] == 1


# ------------------------------------------------------------------
# get_archive_candidates tests
# ------------------------------------------------------------------

def test_get_archive_candidates_empty(db):
    """No candidates when only one active thread exists."""
    tid_a = db.create_thread("Topic A", session_id="s1")
    db.set_current_thread(tid_a)
    candidates = db.get_archive_candidates()
    assert candidates == []


def test_get_archive_candidates_done(db):
    """A done thread (non-current) is a candidate."""
    tid_a = db.create_thread("Topic A", session_id="s1")
    tid_b = db.create_thread("Topic B", session_id="s1")
    db.set_current_thread(tid_a)
    db.update_thread(tid_b, status="done")
    candidates = db.get_archive_candidates()
    assert len(candidates) == 1
    assert candidates[0]["id"] == tid_b


def test_get_archive_candidates_failed(db):
    """A failed thread (non-current) is a candidate."""
    tid_a = db.create_thread("Topic A", session_id="s1")
    tid_b = db.create_thread("Topic B", session_id="s1")
    db.set_current_thread(tid_a)
    db.update_thread(tid_b, status="failed")
    candidates = db.get_archive_candidates()
    assert len(candidates) == 1
    assert candidates[0]["id"] == tid_b


def test_get_archive_candidates_old_inactive(db):
    """A thread inactive > 48 hours (not background/waiting) is a candidate."""
    from datetime import datetime, timezone, timedelta
    tid_a = db.create_thread("Topic A", session_id="s1")
    tid_b = db.create_thread("Topic B", session_id="s1")
    db.set_current_thread(tid_a)
    old_time = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    db.update_thread(tid_b, last_active=old_time, status="active")
    candidates = db.get_archive_candidates()
    assert any(c["id"] == tid_b for c in candidates)



def test_get_archive_candidates_excludes_current(db):
    """Current thread is never a candidate even if it would otherwise qualify."""
    tid_a = db.create_thread("Topic A", session_id="s1")
    db.set_current_thread(tid_a)
    db.update_thread(tid_a, status="done")
    candidates = db.get_archive_candidates()
    assert all(c["id"] != tid_a for c in candidates)


def test_get_archive_candidates_excludes_already_archived(db):
    """Already-archived threads are excluded from candidates."""
    tid_a = db.create_thread("Topic A", session_id="s1")
    tid_b = db.create_thread("Topic B", session_id="s1")
    db.set_current_thread(tid_a)
    db.archive_thread(tid_b)
    candidates = db.get_archive_candidates()
    assert all(c["id"] != tid_b for c in candidates)


def test_get_archive_candidates_background_not_candidate_for_48h_rule(db):
    """Background threads inactive > 48h are NOT candidates (excluded by status filter)."""
    from datetime import datetime, timezone, timedelta
    tid_a = db.create_thread("Topic A", session_id="s1")
    tid_b = db.create_thread("Topic B", session_id="s1")
    db.set_current_thread(tid_a)
    old_time = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    db.update_thread(tid_b, status="background", last_active=old_time, agent_task_id="task_1")
    candidates = db.get_archive_candidates()
    assert all(c["id"] != tid_b for c in candidates)


# ------------------------------------------------------------------
# UUID + label schema tests
# ------------------------------------------------------------------

def test_create_thread_returns_uuid(db):
    """create_thread() returns a UUID string, not a letter."""
    import re
    tid = db.create_thread("My topic", session_id="s1")
    assert re.match(r"^[0-9a-f-]{36}$", tid), f"Expected UUID, got: {tid}"


def test_create_thread_first_label_is_a(db):
    """First thread created gets label 'A'."""
    tid = db.create_thread("My topic", session_id="s1")
    thread = db.get_thread(tid)
    assert thread is not None
    assert thread["label"] == "A"


def test_create_thread_second_label_is_b(db):
    """Second thread gets label 'B'."""
    db.create_thread("First", session_id="s1")
    tid2 = db.create_thread("Second", session_id="s1")
    thread = db.get_thread(tid2)
    assert thread["label"] == "B"


def test_schema_has_id_and_label(db):
    """threads table has 'id' and 'label' columns, not 'thread_id'."""
    with db._connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
    assert "id" in cols
    assert "label" in cols
    assert "thread_id" not in cols


def test_migration_preserves_existing_threads(tmp_path):
    """Existing DBs with thread_id column are migrated: id=letter, label=letter."""
    import sqlite3
    old_db_path = tmp_path / "old.db"
    # Create a legacy-style DB
    with sqlite3.connect(str(old_db_path)) as conn:
        conn.execute("""CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL DEFAULT '',
            topic TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            summary TEXT DEFAULT '',
            key_decisions TEXT DEFAULT '[]',
            open_questions TEXT DEFAULT '[]',
            last_user_intent TEXT DEFAULT '',
            agent_task_id TEXT,
            agent_result TEXT,
            show_in_list INTEGER NOT NULL DEFAULT 1,
            summarized_msg_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '2024-01-01T00:00:00',
            last_active TEXT NOT NULL DEFAULT '2024-01-01T00:00:00'
        )""")
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            token_estimate INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '2024-01-01T00:00:00'
        )""")
        conn.execute("""CREATE TABLE shared_context (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            context_type TEXT NOT NULL,
            content TEXT NOT NULL,
            source_thread TEXT,
            created_at TEXT NOT NULL DEFAULT '2024-01-01T00:00:00'
        )""")
        conn.execute("""CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            message TEXT NOT NULL,
            delivered INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '2024-01-01T00:00:00'
        )""")
        conn.execute("""CREATE TABLE session (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        conn.execute(
            "INSERT INTO threads (thread_id, session_id, topic, created_at, last_active) VALUES ('A', '', 'Legacy Topic', '2024-01-01', '2024-01-01')"
        )
        conn.commit()

    from juggle_db import JuggleDB
    db = JuggleDB(str(old_db_path))
    db.init_db()  # triggers migration

    thread = db.get_thread("A")
    assert thread is not None
    assert thread["id"] == "A"
    assert thread["label"] == "A"
    assert thread["topic"] == "Legacy Topic"


# ------------------------------------------------------------------
# UUID + label Task 2 tests
# ------------------------------------------------------------------

def test_get_thread_by_uuid(db):
    """get_thread() accepts UUID and returns dict with 'id' and 'label'."""
    tid = db.create_thread("My topic", session_id="s1")
    thread = db.get_thread(tid)
    assert thread is not None
    assert thread["id"] == tid
    assert thread["label"] == "A"
    assert thread["topic"] == "My topic"


def test_get_thread_by_label(db):
    """get_thread_by_label('A') returns the thread with label 'A'."""
    tid = db.create_thread("My topic", session_id="s1")
    thread = db.get_thread_by_label("A")
    assert thread is not None
    assert thread["id"] == tid
    assert thread["label"] == "A"


def test_get_thread_by_label_missing(db):
    """get_thread_by_label returns None for unknown label."""
    assert db.get_thread_by_label("Z") is None


def test_get_all_threads_includes_id_and_label(db):
    """get_all_threads() returns dicts with both 'id' and 'label'."""
    tid = db.create_thread("Topic A", session_id="s1")
    threads = db.get_all_threads()
    assert len(threads) == 1
    assert threads[0]["id"] == tid
    assert threads[0]["label"] == "A"


def test_archive_thread_clears_label(db):
    """archive_thread sets label=NULL in addition to status='archived'."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.archive_thread(tid)
    thread = db.get_thread(tid)
    assert thread["status"] == "archived"
    assert thread["label"] is None
    assert thread["show_in_list"] == 0


def test_label_recycled_after_archive(db):
    """Archiving thread A allows label 'A' to be reassigned."""
    tid_a = db.create_thread("First", session_id="s1")
    assert db.get_thread(tid_a)["label"] == "A"

    db.archive_thread(tid_a)

    tid_b = db.create_thread("Second", session_id="s1")
    assert db.get_thread(tid_b)["label"] == "A"  # 'A' recycled


# ------------------------------------------------------------------
# unarchive_thread tests
# ------------------------------------------------------------------

def test_unarchive_thread_restores_show_in_list(db):
    """unarchive_thread sets show_in_list=1."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.archive_thread(tid)
    db.unarchive_thread(tid)
    t = db.get_thread(tid)
    assert t is not None
    assert t["show_in_list"] == 1


def test_unarchive_thread_sets_status_active(db):
    """unarchive_thread sets status='active'."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.archive_thread(tid)
    db.unarchive_thread(tid)
    t = db.get_thread(tid)
    assert t is not None
    assert t["status"] == "active"


def test_unarchive_thread_assigns_label(db):
    """unarchive_thread assigns a non-null label."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.archive_thread(tid)
    label = db.unarchive_thread(tid)
    t = db.get_thread(tid)
    assert t is not None
    assert t["label"] is not None
    assert t["label"] == label
    assert len(label) == 1
    assert label.isalpha()


def test_unarchive_thread_returns_label(db):
    """unarchive_thread return value matches stored label."""
    tid = db.create_thread("Topic A", session_id="s1")
    db.archive_thread(tid)
    returned_label = db.unarchive_thread(tid)
    t = db.get_thread(tid)
    assert t["label"] == returned_label


def test_unarchive_thread_full_cycle(db):
    """create → archive → unarchive produces expected state."""
    tid = db.create_thread("Topic A", session_id="s1")
    assert db.get_thread(tid)["label"] == "A"

    db.archive_thread(tid)
    t = db.get_thread(tid)
    assert t["status"] == "archived"
    assert t["label"] is None
    assert t["show_in_list"] == 0

    label = db.unarchive_thread(tid)
    t = db.get_thread(tid)
    assert t["status"] == "active"
    assert t["show_in_list"] == 1
    assert t["label"] == label
    assert label is not None
