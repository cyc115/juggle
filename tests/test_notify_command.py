import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import pytest
from unittest.mock import patch, MagicMock


def test_notify_inserts_notification_v2(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    tid = db.create_thread("test", session_id="s1")
    db.set_active(True)

    # Simulate session_id in DB
    with db._connect() as conn:
        conn.execute("INSERT OR REPLACE INTO session (key, value) VALUES ('session_id', 'sess-abc')")
        conn.commit()

    args = MagicMock()
    args.thread_id = tid
    args.message = "Research complete, ready to review"

    with patch("juggle_cli_common.get_db", return_value=db):
        from juggle_cmd_agents import cmd_notify
        cmd_notify(args)

    notifs = db.get_notifications_for_session("sess-abc")
    assert len(notifs) == 1
    assert notifs[0]["message"] == "Research complete, ready to review"
    assert notifs[0]["thread_id"] == tid
