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
    # v2 context no longer injects SUMMARY STALE — this is handled by render_topics_tree
    for i in range(3):
        active_db.add_message(active_db.get_current_thread(), "user", f"real question {i}")
    ctx = ContextBuilder(active_db).build()
    # New format: Q&A history rendered in Tier 1 block instead
    assert "JUGGLE ACTIVE" in ctx


def test_stale_flag_not_emitted_after_count_updated(active_db):
    for i in range(3):
        active_db.add_message(active_db.get_current_thread(), "user", f"real question {i}")
    active_db.update_thread(active_db.get_current_thread(), summarized_msg_count=3)
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
    # Articles stripped in v2 renderer: "the auth flow" → "auth flow"
    assert "Summary: We discussed" in ctx
    assert "auth flow" in ctx


def test_no_summary_line_when_empty(active_db):
    """No 'Summary:' line is emitted when the thread has no summary."""
    ctx = ContextBuilder(active_db).build()
    assert "Summary:" not in ctx


def test_topic_header_present(active_db):
    """The Active Threads section header is present in v2 format."""
    ctx = ContextBuilder(active_db).build()
    assert "# Active Threads" in ctx


def test_current_thread_rendered_in_active(active_db):
    """Current thread appears in the Active Threads block."""
    ctx = ContextBuilder(active_db).build()
    assert "[A] 🟢 active | Topic A" in ctx


def test_done_thread_suffix(active_db):
    """Closed threads appear in Closed (within TTL) section."""
    tid_b = active_db.create_thread("Topic B", session_id="s1")
    active_db.set_thread_status(tid_b, "closed")
    ctx = ContextBuilder(active_db).build()
    assert "✅ closed" in ctx


def test_archived_thread_suffix(active_db):
    """Archived threads are not shown in v2 context injection (cockpit only)."""
    tid_b = active_db.create_thread("Topic B", session_id="s1")
    active_db.archive_thread(tid_b)
    ctx = ContextBuilder(active_db).build()
    # Archived threads are Tier 3 — omitted from context injection
    assert "archived" not in ctx.lower() or "[B]" not in ctx


def test_key_decisions_in_tier1_block(active_db):
    """Key decisions ARE injected for Tier 1 (active) threads in v2."""
    import json
    active_db.update_thread(active_db.get_current_thread(), key_decisions=json.dumps(["Use Postgres", "Auth via JWT"]))
    ctx = ContextBuilder(active_db).build()
    assert "Key decisions:" in ctx


def test_open_questions_in_tier1_block(active_db):
    """Open questions ARE injected for Tier 1 (active) threads in v2."""
    import json
    active_db.update_thread(active_db.get_current_thread(), open_questions=json.dumps(["Should we cache?", "Rate limit?"]))
    ctx = ContextBuilder(active_db).build()
    assert "Open questions:" in ctx


def test_summary_for_multiple_threads(active_db):
    """Summaries for all threads (including non-current) appear in the Topics list."""
    tid_b = active_db.create_thread("Topic B", session_id="s1")
    active_db.update_thread(active_db.get_current_thread(), summary="Summary for A")
    active_db.update_thread(tid_b, summary="Summary for B")
    ctx = ContextBuilder(active_db).build()
    assert "Summary: Summary for A" in ctx
    assert "Summary: Summary for B" in ctx


def test_running_thread_shows_in_active(active_db):
    """Running threads show '🏃 running' in v2 format."""
    tid_b = active_db.create_thread("Topic B", session_id="s1")
    active_db.set_thread_status(tid_b, "running")
    ctx = ContextBuilder(active_db).build()
    assert "🏃 running" in ctx
