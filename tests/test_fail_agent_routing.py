"""Tests for Task 8 fail-agent transient/persistent routing."""
import argparse
import pytest
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    import juggle_cli_common as common
    monkeypatch.setattr(common, "get_db", lambda: d)
    return d


def test_fail_agent_persistent_creates_action_item_and_closes(db):
    from juggle_cmd_agents import cmd_fail_agent
    tid = db.create_thread("t", session_id="s")
    db.set_thread_status(tid, "running")
    args = argparse.Namespace(thread_id=tid, error="AuthError: bad key",
                               failure_type="persistent", max_retries=0)
    cmd_fail_agent(args)
    items = db.get_open_action_items()
    assert len(items) == 1
    assert items[0]["priority"] == "high"
    assert items[0]["type"] == "failure"
    assert db.get_thread(tid)["status"] == "closed"


def test_fail_agent_transient_keeps_running_no_action_item(db):
    from juggle_cmd_agents import cmd_fail_agent
    tid = db.create_thread("t", session_id="s")
    db.set_thread_status(tid, "running")
    args = argparse.Namespace(thread_id=tid, error="ETIMEDOUT",
                               failure_type="transient", max_retries=3)
    cmd_fail_agent(args)
    assert db.get_open_action_items() == []
    assert db.get_thread(tid)["status"] == "running"


def test_fail_agent_autoclassifies_network_as_transient(db):
    from juggle_cmd_agents import _classify_failure
    assert _classify_failure("ETIMEDOUT connecting to api.example.com") == "transient"
    assert _classify_failure("rate limit exceeded") == "transient"
    assert _classify_failure("Network unreachable") == "transient"


def test_fail_agent_autoclassifies_auth_as_persistent(db):
    from juggle_cmd_agents import _classify_failure
    assert _classify_failure("401 Unauthorized") == "persistent"
    assert _classify_failure("FileNotFoundError: /tmp/x") == "persistent"
    assert _classify_failure("SyntaxError: invalid token") == "persistent"
