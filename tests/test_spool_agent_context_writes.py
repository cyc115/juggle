"""Task 5 (spool-single-writer plan): cmd_graph_mark_task early-return to
spool in agent context, dropping init=True — task_id is NOT resolved through
resolve_thread_id_for_spool (see the plan's explicit rebuttal in Task 5)."""
from types import SimpleNamespace

import pytest

import juggle_cmd_graph as cg
from dbops.spool import read_pending


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
