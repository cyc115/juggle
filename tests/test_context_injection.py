"""Tests for Task 5 tiered context injection."""

import json
import pytest
from juggle_db import JuggleDB
from juggle_context import _build


@pytest.fixture(autouse=True)
def _clear_agent_env(monkeypatch):
    """Prevent JUGGLE_IS_AGENT=1 (set in agent Claude sessions) from tainting orchestrator-path tests."""
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    d.set_active(True)
    d._set_session_key_external("session_id", "sessA")
    return d


def test_tier1_active_thread_renders_full_block(db):
    tid = db.create_thread("Cockpit refactor", session_id="sessA")
    db.update_thread(
        tid,
        key_decisions=json.dumps(["2026-04-16 11:00: Chose Rich over Textual"]),
    )
    db.set_current_thread(tid)
    out = _build(db)
    assert "[AA] 🟢 open | Cockpit refactor" in out
    assert "Key decisions:" in out
    assert "Chose Rich over Textual" in out


def test_tier1_running_thread_renders_full(db):
    tid = db.create_thread("Worker", session_id="sessA")
    db.set_thread_status(tid, "running")
    out = _build(db)
    assert "[AA] 🏃 running | Worker" in out


def test_tier2_closed_thread_within_ttl_one_line(db):
    tid = db.create_thread("tax-submit", session_id="sessA")
    db.set_thread_status(tid, "closed")
    out = _build(db)
    assert "[AA] ✅ closed  | tax-submit" in out
    # Tier 2 should NOT include Summary/Key decisions/Q&A block for closed threads
    lines = out.splitlines()
    idx = next(i for i, ln in enumerate(lines) if "[AA] ✅ closed" in ln)
    # Next non-empty line must not start with "Summary:"
    for nxt in lines[idx + 1 : idx + 3]:
        assert not nxt.lstrip().startswith("Summary:")


def test_tier3_archived_thread_omitted(db):
    tid = db.create_thread("old", session_id="sessA")
    db.archive_thread(tid)
    out = _build(db)
    assert "archived" not in out.lower() or "[AA]" not in out  # not injected


def test_action_items_rendered_at_top(db):
    tid = db.create_thread("t", session_id="sessA")
    db.add_action_item(
        thread_id=tid,
        message="push to prod pending",
        type_="manual_step",
        priority="high",
    )
    out = _build(db)
    assert "⚡" in out
    assert "HIGH" in out
    assert "push to prod pending" in out
    assert "(thread: [AA])" in out


def test_notifications_rendered_for_current_session(db):
    db.add_notification_v2(thread_id=None, message="merged PR #412", session_id="sessA")
    out = _build(db)
    assert "✓ merged PR #412" in out


def test_notifications_from_other_sessions_excluded(db):
    db.add_notification_v2(
        thread_id=None, message="old session noise", session_id="sessOLD"
    )
    out = _build(db)
    assert "old session noise" not in out


def test_timestamps_are_minute_precision(db):
    tid = db.create_thread("t", session_id="sessA")
    db.update_thread(
        tid, key_decisions=json.dumps(["2026-04-17 14:32:59: Bad"])
    )
    out = _build(db)
    # No seconds should leak through
    import re

    assert not re.search(r"\d{2}:\d{2}:\d{2}", out)


# ---------------------------------------------------------------------------
# Notification watermark tests
# ---------------------------------------------------------------------------

def test_first_message_gets_last_5_notifications(db, monkeypatch):
    """Session with no watermark gets at most 5 most-recent notifications."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-sess-first")
    for i in range(7):
        db.add_notification_v2(thread_id=None, message=f"notif-{i}", session_id="sessA")
    out = _build(db)
    matches = [line for line in out.splitlines() if line.startswith("✓ notif-")]
    assert len(matches) <= 5


def test_subsequent_message_gets_only_new_notifications(db, monkeypatch):
    """Session with watermark only sees notifications added after it."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-sess-sub")
    db.add_notification_v2(thread_id=None, message="old-notif", session_id="sessA")
    # First call sets watermark
    _build(db)
    # Add a new notification after watermark is set
    db.add_notification_v2(thread_id=None, message="new-notif", session_id="sessA")
    out = _build(db)
    assert "new-notif" in out
    assert "old-notif" not in out


def test_multiple_sessions_independent_watermarks(db, monkeypatch):
    """Two CLAUDE_CODE_SESSION_IDs each get their own independent watermark."""
    db.add_notification_v2(thread_id=None, message="shared-notif", session_id="sessA")

    # Session A sees it (first message, sets watermark)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-sess-A")
    out_a1 = _build(db)
    assert "shared-notif" in out_a1

    # Session A second call: watermark set, no new notifs → nothing shown
    out_a2 = _build(db)
    assert "shared-notif" not in out_a2

    # Session B (different CLAUDE_CODE_SESSION_ID) has no watermark yet → sees the notif
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-sess-B")
    out_b = _build(db)
    assert "shared-notif" in out_b
