"""Tests for juggle_context.py ContextBuilder."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB
from juggle_context import ContextBuilder


@pytest.fixture
def active_db(tmp_path):
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    db.set_active(True)
    tid = db.create_thread("Topic A", session_id="s1")
    db.set_current_thread(tid)
    return db


def test_no_stale_flag_when_fresh(active_db):
    # 2 messages — below threshold of 3
    active_db.add_message(active_db.get_current_thread(), "user", "real question one")
    active_db.add_message(active_db.get_current_thread(), "user", "real question two")
    ctx = ContextBuilder(active_db).build()
    assert "SUMMARY STALE" not in ctx


def test_stale_flag_emitted_at_threshold(active_db):
    for i in range(3):
        active_db.add_message(active_db.get_current_thread(), "user", f"real question {i}")
    ctx = ContextBuilder(active_db).build()
    assert "[SUMMARY STALE: 3 new messages" in ctx


def test_stale_flag_not_emitted_after_count_updated(active_db):
    for i in range(3):
        active_db.add_message(active_db.get_current_thread(), "user", f"real question {i}")
    active_db.set_summarized_count(active_db.get_current_thread(), 3)
    ctx = ContextBuilder(active_db).build()
    assert "SUMMARY STALE" not in ctx


def test_junk_messages_not_counted(active_db):
    active_db.add_message(active_db.get_current_thread(), "user", "real question one")
    active_db.add_message(active_db.get_current_thread(), "user", "/juggle:show-topics")
    active_db.add_message(active_db.get_current_thread(), "user", "<task-notification>...</task-notification>")
    # Only 1 substantive message — below threshold
    ctx = ContextBuilder(active_db).build()
    assert "SUMMARY STALE" not in ctx


# ---------------------------------------------------------------------------
# New format tests: summary-only injection (no "Recent conversation" block)
# ---------------------------------------------------------------------------

def test_no_recent_conversation_block(active_db):
    """The old 'Recent conversation' block must not appear in output."""
    active_db.add_message(active_db.get_current_thread(), "user", "hello")
    active_db.add_message(active_db.get_current_thread(), "assistant", "hi there")
    ctx = ContextBuilder(active_db).build()
    assert "Recent conversation" not in ctx


def test_summary_appears_under_topic(active_db):
    """Thread summary is shown inline under the topic label in the Topics list."""
    active_db.update_thread(active_db.get_current_thread(), summary="We discussed the auth flow.")
    ctx = ContextBuilder(active_db).build()
    assert "Summary: We discussed the auth flow." in ctx


def test_no_summary_line_when_empty(active_db):
    """No 'Summary:' line is emitted when the thread has no summary."""
    ctx = ContextBuilder(active_db).build()
    assert "Summary:" not in ctx


def test_topic_header_present(active_db):
    """The Topics: section header is present."""
    ctx = ContextBuilder(active_db).build()
    assert "Topics:" in ctx


def test_current_thread_marked_with_you_are_here(active_db):
    """Current thread is marked with '← you are here'."""
    ctx = ContextBuilder(active_db).build()
    assert "← you are here" in ctx


def test_done_thread_suffix(active_db):
    """Done threads are labelled with '✓ done'."""
    tid_b = active_db.create_thread("Topic B", session_id="s1")
    active_db.update_thread(tid_b, status="done")
    ctx = ContextBuilder(active_db).build()
    assert "✓ done" in ctx


def test_archived_thread_suffix(active_db):
    """Archived threads are labelled with '🗄️ archived'."""
    tid_b = active_db.create_thread("Topic B", session_id="s1")
    active_db.update_thread(tid_b, status="archived")
    ctx = ContextBuilder(active_db).build()
    assert "🗄️ archived" in ctx


def test_no_key_decisions_block(active_db):
    """Key decisions are no longer injected as a top-level block."""
    import json
    active_db.update_thread(active_db.get_current_thread(), key_decisions=json.dumps(["Use Postgres", "Auth via JWT"]))
    ctx = ContextBuilder(active_db).build()
    assert "Key decisions:" not in ctx


def test_no_open_questions_block(active_db):
    """Open questions are no longer injected as a top-level block."""
    import json
    active_db.update_thread(active_db.get_current_thread(), open_questions=json.dumps(["Should we cache?", "Rate limit?"]))
    ctx = ContextBuilder(active_db).build()
    assert "Open questions:" not in ctx


def test_summary_for_multiple_threads(active_db):
    """Summaries for all threads (including non-current) appear in the Topics list."""
    tid_b = active_db.create_thread("Topic B", session_id="s1")
    active_db.update_thread(active_db.get_current_thread(), summary="Summary for A")
    active_db.update_thread(tid_b, summary="Summary for B")
    ctx = ContextBuilder(active_db).build()
    assert "Summary: Summary for A" in ctx
    assert "Summary: Summary for B" in ctx


def test_background_thread_suffix(active_db):
    """Background threads show 'agent working...' suffix."""
    tid_b = active_db.create_thread("Topic B", session_id="s1")
    active_db.update_thread(tid_b, status="background")
    ctx = ContextBuilder(active_db).build()
    assert "agent working..." in ctx
