"""Tests for Task 5 tiered context injection."""
import json
import pytest
from juggle_db import JuggleDB
from juggle_context import _build


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    d.set_active(True)
    d._set_session_key_external("session_id", "sessA")
    return d


def test_tier1_active_thread_renders_full_block(db):
    tid = db.create_thread("Cockpit refactor", session_id="sessA")
    db.update_thread(tid, summary="Refactored TUI with new layout. PR merged.",
                     key_decisions=json.dumps(["2026-04-16 11:00: Chose Rich over Textual"]))
    db.set_current_thread(tid)
    out = _build(db)
    assert "[A] 🟢 active | Cockpit refactor" in out
    assert "Summary: Refactored TUI" in out
    assert "Key decisions:" in out
    assert "Chose Rich over Textual" in out


def test_tier1_running_thread_renders_full(db):
    tid = db.create_thread("Worker", session_id="sessA")
    db.set_thread_status(tid, "running")
    out = _build(db)
    assert "[A] 🏃 running | Worker" in out


def test_tier2_closed_thread_within_ttl_one_line(db):
    tid = db.create_thread("tax-submit", session_id="sessA")
    db.set_thread_status(tid, "closed")
    out = _build(db)
    assert "[A] ✅ closed  | tax-submit" in out
    # Tier 2 should NOT include Summary/Key decisions/Q&A block for closed threads
    lines = out.splitlines()
    idx = next(i for i, l in enumerate(lines) if "[A] ✅ closed" in l)
    # Next non-empty line must not start with "Summary:"
    for nxt in lines[idx+1:idx+3]:
        assert not nxt.lstrip().startswith("Summary:")


def test_tier3_archived_thread_omitted(db):
    tid = db.create_thread("old", session_id="sessA")
    db.archive_thread(tid)
    out = _build(db)
    assert "archived" not in out.lower() or "[A]" not in out  # not injected


def test_action_items_rendered_at_top(db):
    tid = db.create_thread("t", session_id="sessA")
    db.add_action_item(thread_id=tid, message="push to prod pending",
                       type_="manual_step", priority="high")
    out = _build(db)
    assert "⚡" in out
    assert "HIGH" in out
    assert "push to prod pending" in out
    assert "(thread: [A])" in out


def test_notifications_rendered_for_current_session(db):
    db.add_notification_v2(thread_id=None, message="merged PR #412", session_id="sessA")
    out = _build(db)
    assert "✓ merged PR #412" in out


def test_notifications_from_other_sessions_excluded(db):
    db.add_notification_v2(thread_id=None, message="old session noise", session_id="sessOLD")
    out = _build(db)
    assert "old session noise" not in out


def test_timestamps_are_minute_precision(db):
    tid = db.create_thread("t", session_id="sessA")
    db.update_thread(tid, summary="x", key_decisions=json.dumps(["2026-04-17 14:32:59: Bad"]))
    out = _build(db)
    # No seconds should leak through
    import re
    assert not re.search(r"\d{2}:\d{2}:\d{2}", out)
