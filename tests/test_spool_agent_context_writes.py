"""Tasks 4-5 (spool-single-writer plan): CLI commands early-return to spool in
agent context instead of opening the DB read-write.

Task 4: cmd_request_action/cmd_ack_action/cmd_notify — action_create/
action_notify resolve thread_id via resolve_thread_id_for_spool() first
(action_ack has no thread_id, so it skips resolution).

Task 5: cmd_graph_mark_task — dropping init=True; task_id is NOT resolved
through resolve_thread_id_for_spool (see the plan's explicit rebuttal in
Task 5)."""
import json
from types import SimpleNamespace

import pytest

import juggle_cli_common as cli_common
import juggle_cmd_graph as cg
from dbops.spool import read_pending
from juggle_cmd_agents import cmd_ack_action, cmd_notify, cmd_request_action


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in ("JUGGLE_IS_AGENT", "JUGGLE_ORCHESTRATOR", "JUGGLE_AGENT_WORKTREE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def _args(**overrides):
    base = dict(task_id="T1", fail=False, handoff=None, db_path=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_mark_task_spools_instead_of_writing_db_in_agent_context(monkeypatch, tmp_path):
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    spool_d = tmp_path / "spool"
    monkeypatch.setattr("juggle_spool_paths.spool_dir", lambda: spool_d)

    def _boom(*a, **kw):
        raise AssertionError("get_db must not be called when spooling")

    monkeypatch.setattr(cg, "get_db", _boom)

    cg.cmd_graph_mark_task(_args(task_id="T1", handoff="did stuff"))

    events = read_pending(spool_d)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "graph_mark_task"
    assert ev.args["task_id"] == "T1"
    assert ev.args["fail"] is False
    assert ev.args["handoff"] == "did stuff"


def test_mark_task_spool_event_does_not_resolve_task_id_as_thread(monkeypatch, tmp_path):
    """task_id must NOT be run through resolve_thread_id_for_spool — it is a
    task id, not a thread label/uuid."""
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    spool_d = tmp_path / "spool"
    monkeypatch.setattr("juggle_spool_paths.spool_dir", lambda: spool_d)
    monkeypatch.setattr(cg, "get_db", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("get_db must not be called when spooling")))

    def _boom_resolve(*a, **kw):
        raise AssertionError("resolve_thread_id_for_spool must not be called for task_id")

    monkeypatch.setattr("juggle_spool_cli_common.resolve_thread_id_for_spool", _boom_resolve)

    cg.cmd_graph_mark_task(_args(task_id="AB"))

    events = read_pending(spool_d)
    assert events[0].args["task_id"] == "AB"


def test_mark_task_still_writes_db_when_not_agent_context(monkeypatch):
    """Non-agent context keeps the existing init=True DB write path."""
    calls = []

    class _FakeTask(dict):
        pass

    class _FakeDB:
        pass

    def _fake_get_db(db_path, init=False):
        calls.append(init)
        return _FakeDB()

    monkeypatch.setattr(cg, "get_db", _fake_get_db)
    monkeypatch.setattr(cg.db_graph, "get_task", lambda db, tid: {"topic_id": None})
    monkeypatch.setattr(cg.db_graph, "mark_completion", lambda *a, **kw: "verified")

    cg.cmd_graph_mark_task(_args(task_id="T1"))

    assert calls == [True]


@pytest.fixture
def spool_env(monkeypatch, tmp_path):
    """Force agent context + redirect the spool dir and DB (for the readonly
    resolve helper) to isolated tmp paths — no real thread ever matches "AB"/"CD"."""
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    config_dir = tmp_path / "juggle_config"
    monkeypatch.setenv("JUGGLE_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(cli_common, "DB_PATH", tmp_path / "empty.db")
    return config_dir / "spool"


def _read_only_event(spool_dir):
    files = [p for p in spool_dir.glob("*.json")]
    assert len(files) == 1
    return json.loads(files[0].read_text())


def test_cmd_request_action_spools_when_in_agent_context(spool_env, capsys):
    args = SimpleNamespace(thread_id="AB", message="do the thing",
                            type="manual_step", priority="high")
    cmd_request_action(args)
    payload = _read_only_event(spool_env)
    assert payload["type"] == "action_create"
    assert payload["thread_id"] == "AB"  # best-effort resolve, no DB to match against
    assert payload["args"] == {
        "message": "do the thing", "type": "manual_step", "priority": "high",
    }


def test_cmd_request_action_validates_priority_before_spooling(spool_env, capsys):
    args = SimpleNamespace(thread_id="AB", message="x", type="manual_step",
                            priority="urgent")
    with pytest.raises(SystemExit):
        cmd_request_action(args)
    assert list(spool_env.glob("*.json")) == []


def test_cmd_request_action_does_not_spool_outside_agent_context(monkeypatch, tmp_path):
    assert cli_common.should_spool() is False
    # No DB configured — calling through would raise/exit rather than spool.
    args = SimpleNamespace(thread_id="AB", message="x", type="manual_step",
                            priority="normal")
    with pytest.raises(SystemExit):
        cmd_request_action(args)


def test_cmd_ack_action_spools_when_in_agent_context(spool_env, capsys):
    args = SimpleNamespace(action_id="42")
    cmd_ack_action(args)
    payload = _read_only_event(spool_env)
    assert payload["type"] == "action_ack"
    assert payload["thread_id"] == ""  # ack has no thread_id — resolution skipped
    assert payload["args"] == {"action_id": 42}


def test_cmd_ack_action_validates_numeric_id_before_spooling(spool_env, capsys):
    args = SimpleNamespace(action_id="not-a-number")
    with pytest.raises(SystemExit):
        cmd_ack_action(args)
    assert list(spool_env.glob("*.json")) == []


def test_cmd_notify_spools_when_in_agent_context(spool_env, capsys):
    args = SimpleNamespace(thread_id="CD", message="hello")
    cmd_notify(args)
    payload = _read_only_event(spool_env)
    assert payload["type"] == "action_notify"
    assert payload["thread_id"] == "CD"
    assert payload["args"] == {"message": "hello"}
