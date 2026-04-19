"""Tests for Task 3 completion CLI commands."""
import argparse
import pytest
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "juggle.db"
    d = JuggleDB(db_path=str(path))
    d.init_db()
    # Force shared singleton used by cmd_* handlers to this test DB
    import juggle_cli_common as common
    monkeypatch.setattr(common, "get_db", lambda: d)
    return d


def test_add_notification_v2_creates_row(db):
    tid = db.create_thread("t", session_id="s")
    nid = db.add_notification_v2(thread_id=tid, message="merged PR", session_id="sess1")
    rows = db.get_notifications_for_session("sess1")
    assert len(rows) == 1
    assert rows[0]["message"] == "merged PR"


def test_add_action_item_creates_open_row(db):
    tid = db.create_thread("t", session_id="s")
    aid = db.add_action_item(thread_id=tid, message="push to prod",
                              type_="manual_step", priority="high")
    items = db.get_open_action_items()
    assert len(items) == 1
    assert items[0]["message"] == "push to prod"
    assert items[0]["priority"] == "high"


def test_dismiss_action_item(db):
    aid = db.add_action_item(thread_id=None, message="x", type_="question", priority="normal")
    db.dismiss_action_item(aid)
    assert db.get_open_action_items() == []


def test_cmd_complete_agent_creates_notification_and_closes_thread(db, capsys):
    from juggle_cmd_agents import cmd_complete_agent
    tid = db.create_thread("t", session_id="sessA")
    # Set agent_task_id so the command recognises this as an agent-completed thread
    db.update_thread(tid, agent_task_id="task-1", status="running")
    db._set_session_key_external("session_id", "sessA")
    args = argparse.Namespace(thread_id=tid, result_summary="merged PR #412",
                               retain_text=None, open_questions=None)
    cmd_complete_agent(args)
    assert db.get_thread(tid)["status"] == "closed"
    notifs = db.get_notifications_for_session("sessA")
    assert any("merged PR #412" in n["message"] for n in notifs)


def test_cmd_complete_agent_converts_open_questions_to_action_items(db):
    import json
    from juggle_cmd_agents import cmd_complete_agent
    tid = db.create_thread("t", session_id="sessA")
    db.update_thread(tid, agent_task_id="task-2", status="running",
                     open_questions=json.dumps(["Push to prod?", "Also bump version?"]))
    db._set_session_key_external("session_id", "sessA")
    args = argparse.Namespace(thread_id=tid, result_summary="done", retain_text=None,
                               open_questions=None)
    cmd_complete_agent(args)
    items = db.get_open_action_items()
    assert len(items) == 2
    msgs = {i["message"] for i in items}
    assert msgs == {"Push to prod?", "Also bump version?"}
    # open_questions cleared
    assert json.loads(db.get_thread(tid)["open_questions"] or "[]") == []


def test_cmd_request_action_creates_action_item_keeps_state(db):
    from juggle_cmd_agents import cmd_request_action
    tid = db.create_thread("t", session_id="sessA")
    db.set_thread_status(tid, "running")
    args = argparse.Namespace(thread_id=tid, message="push to prod pending",
                               type="manual_step", priority="high")
    cmd_request_action(args)
    # Thread remains running; action_items row created
    assert db.get_thread(tid)["status"] == "running"
    items = db.get_open_action_items()
    assert len(items) == 1
    assert items[0]["message"] == "push to prod pending"
    assert items[0]["priority"] == "high"


def test_cmd_ack_action_dismisses(db, capsys):
    from juggle_cmd_agents import cmd_ack_action
    aid = db.add_action_item(thread_id=None, message="x", type_="question", priority="normal")
    args = argparse.Namespace(action_id=aid)
    cmd_ack_action(args)
    assert db.get_open_action_items() == []


def test_cmd_close_thread_sets_closed_state(db, capsys):
    from juggle_cmd_threads import cmd_close_thread
    tid = db.create_thread("t", session_id="sessA")
    args = argparse.Namespace(thread_id=tid)
    cmd_close_thread(args)
    assert db.get_thread(tid)["status"] == "closed"
