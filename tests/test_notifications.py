"""Tests for irl-backbone R1: emit_event() seam + kind/handled_by columns.

Behavior-preserving: existing add_notification_v2 callers must keep working
byte-identically (same message text, same rows returned by existing
readers) while every row now also carries a kind/handled_by pair.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB


def _db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


def test_notifications_v2_has_kind_and_handled_by_columns(tmp_path):
    d = _db(tmp_path)
    with d._connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(notifications_v2)")}
    assert "kind" in cols
    assert "handled_by" in cols


def test_emit_event_stores_kind_and_handled_by(tmp_path):
    d = _db(tmp_path)
    tid = d.create_thread("t", session_id="s")
    nid = d.emit_event(tid, "hello", "sess1", kind="agent_complete", handled_by="orchestrator")
    with d._connect() as conn:
        row = conn.execute(
            "SELECT message, kind, handled_by FROM notifications_v2 WHERE id = ?", (nid,)
        ).fetchone()
    assert row["message"] == "hello"
    assert row["kind"] == "agent_complete"
    assert row["handled_by"] == "orchestrator"


def test_emit_event_defaults_to_legacy_kind(tmp_path):
    d = _db(tmp_path)
    tid = d.create_thread("t", session_id="s")
    nid = d.emit_event(tid, "hello", "sess1")
    with d._connect() as conn:
        row = conn.execute(
            "SELECT kind, handled_by FROM notifications_v2 WHERE id = ?", (nid,)
        ).fetchone()
    assert row["kind"] == "legacy"
    assert row["handled_by"] == ""


def test_add_notification_v2_routes_through_emit_event_byte_identical(tmp_path):
    """add_notification_v2 must keep returning identical rows/text and land
    in the legacy kind bucket via the new emit_event seam."""
    d = _db(tmp_path)
    tid = d.create_thread("t", session_id="s")
    nid = d.add_notification_v2(tid, "merged PR #412", "sess1")

    rows = d.get_notifications_for_session("sess1")
    assert len(rows) == 1
    assert rows[0]["id"] == nid
    assert rows[0]["message"] == "merged PR #412"
    assert rows[0]["thread_id"] == tid

    with d._connect() as conn:
        row = conn.execute(
            "SELECT kind, handled_by FROM notifications_v2 WHERE id = ?", (nid,)
        ).fetchone()
    assert row["kind"] == "legacy"
    assert row["handled_by"] == ""
