"""juggle_dispatch_stamp — shared "task was sent" stamp seam.

2026-07-13 completed-agents-never-decommission regression: every send-task call
site must stamp last_task/last_send_task_pane_hash/last_send_task_at together so
the watchdog classifier never sees a dispatched agent as "never dispatched".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB


def test_record_dispatch_stamps_all_three_fields(tmp_path):
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    agent_id = db.create_agent(role="coder", pane_id="%1")

    from juggle_dispatch_stamp import record_dispatch

    record_dispatch(db, agent_id, last_task="do the work", pane_hash="abc123")

    agent = db.get_agent(agent_id)
    assert agent["last_task"] == "do the work"
    assert agent["last_send_task_pane_hash"] == "abc123"
    assert agent["last_send_task_at"] is not None


def test_record_dispatch_forwards_extra_fields(tmp_path):
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    agent_id = db.create_agent(role="coder", pane_id="%1")

    from juggle_dispatch_stamp import record_dispatch

    record_dispatch(
        db, agent_id, last_task="do it", pane_hash="h1", harness="claude", model="m1",
    )

    agent = db.get_agent(agent_id)
    assert agent["harness"] == "claude"
    assert agent["model"] == "m1"
