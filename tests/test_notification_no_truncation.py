"""Regression pin: notification text must never be truncated.

Incident 2026-06-16: long notifications were truncated to 280 chars with
a '…(full detail: get-messages <id>)' pointer suffix, hiding content inline.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


LONG_MESSAGE = "A" * 300 + " — important detail that must not be lost"


def test_add_notification_v2_stores_full_text(tmp_path):
    """add_notification_v2 must store the full message, not a truncated version."""
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    tid = db.create_thread("test", session_id="s1")
    with db._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO session (key, value) VALUES ('session_id', 'sess-x')"
        )
        conn.commit()

    db.add_notification_v2(tid, LONG_MESSAGE, "sess-x")

    notifs = db.get_notifications_for_session("sess-x")
    assert len(notifs) == 1
    assert notifs[0]["message"] == LONG_MESSAGE
    assert "full detail" not in notifs[0]["message"]


def test_add_action_item_stores_full_text(tmp_path):
    """add_action_item must store the full message without truncation."""
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    tid = db.create_thread("test", session_id="s1")

    db.add_action_item(tid, LONG_MESSAGE, "review")

    items = db.get_open_action_items()
    assert len(items) == 1
    assert items[0]["message"] == LONG_MESSAGE
    assert "full detail" not in items[0]["message"]
