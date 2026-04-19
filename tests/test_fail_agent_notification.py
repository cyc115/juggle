import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import pytest
from unittest.mock import patch, MagicMock


def _make_db(tmp_path, session_id="sess-1"):
    from juggle_db import JuggleDB
    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    with db._connect() as conn:
        conn.execute("INSERT OR REPLACE INTO session (key, value) VALUES ('session_id', ?)", (session_id,))
        conn.commit()
    return db


def test_unrecoverable_creates_high_action_item_and_notification(tmp_path):
    db = _make_db(tmp_path, "sess-1")
    tid = db.create_thread("test", session_id="sess-1")
    # Pre-existing action item that should be dismissed
    db.add_action_item(tid, "old action", type_="manual_step", priority="normal")

    args = MagicMock()
    args.thread_id = tid
    args.error = "pane died"
    args.failure_type = "persistent"
    args.recovery_dispatched = False
    args.max_retries = 0

    with patch("juggle_cli_common.get_db", return_value=db):
        from juggle_cmd_agents import cmd_fail_agent
        cmd_fail_agent(args)

    # Old action item dismissed
    items = db.get_open_action_items()
    assert all("old action" not in i["message"] for i in items)
    # New HIGH action item created
    assert any(i["priority"] == "high" and "pane died" in i["message"] for i in items)
    # Notification fired
    notifs = db.get_notifications_for_session("sess-1")
    assert any("✗" in n["message"] and "pane died" in n["message"] for n in notifs)
    # Thread closed
    assert db.get_thread(tid)["status"] == "closed"


def test_recovery_dispatched_notifies_no_action_item(tmp_path):
    db = _make_db(tmp_path, "sess-2")
    tid = db.create_thread("test", session_id="sess-2")
    db.add_action_item(tid, "old action", type_="manual_step", priority="normal")

    args = MagicMock()
    args.thread_id = tid
    args.error = "pane timed out"
    args.failure_type = "persistent"
    args.recovery_dispatched = True
    args.max_retries = 0

    with patch("juggle_cli_common.get_db", return_value=db):
        from juggle_cmd_agents import cmd_fail_agent
        cmd_fail_agent(args)

    # Old action item dismissed, no new one
    assert len(db.get_open_action_items()) == 0
    # Notification fired with recovery indicator
    notifs = db.get_notifications_for_session("sess-2")
    assert any("⟳" in n["message"] for n in notifs)
    # Thread stays running
    assert db.get_thread(tid)["status"] == "running"
