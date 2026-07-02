"""Task 3 (spool-single-writer plan): cmd_complete_agent/cmd_fail_agent
early-return to spool in agent context, resolving thread_id via
resolve_thread_id_for_spool() BEFORE write_event (DA Resolution #6)."""
from argparse import Namespace

import pytest

import juggle_cli_common as cli_common
import juggle_cmd_agents_complete as complete_mod
from dbops.spool import read_pending


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in ("JUGGLE_IS_AGENT", "JUGGLE_ORCHESTRATOR", "JUGGLE_AGENT_WORKTREE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def spooling(monkeypatch, tmp_path):
    """should_spool() → True, spool_dir() resolves under tmp_path, and
    resolve_thread_id_for_spool best-effort-passthroughs (no real DB to hit)."""
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    monkeypatch.setattr("juggle_spool_paths.spool_dir", lambda: tmp_path / "spool")
    monkeypatch.setattr(cli_common, "_db_path", lambda: tmp_path / "does-not-exist.db",
                         raising=False)
    return tmp_path / "spool"


def _blow_up_if_db_opened(*_a, **_k):
    raise AssertionError("DB must not be opened on the spool early-return path")


def test_complete_agent_spools_and_never_opens_db(spooling, monkeypatch):
    monkeypatch.setattr(cli_common, "get_db", _blow_up_if_db_opened)
    args = Namespace(
        thread_id="AB", result_summary="did the thing",
        retain_text="key finding", open_questions=None,
        handoff="files: x.py", role="coder",
    )

    complete_mod.cmd_complete_agent(args)

    events = read_pending(spooling)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "agent_complete"
    assert ev.args["thread_id"] == "AB"
    assert ev.args["result_summary"] == "did the thing"
    assert ev.args["handoff"] == "files: x.py"
    assert ev.args["role"] == "coder"


def test_complete_agent_resolves_thread_id_before_writing(spooling, monkeypatch):
    monkeypatch.setattr(cli_common, "get_db", _blow_up_if_db_opened)
    monkeypatch.setattr(cli_common, "resolve_thread_id_for_spool", lambda s: "resolved-uuid-1234")
    args = Namespace(
        thread_id="AB", result_summary="ok", retain_text=None,
        open_questions=None, handoff=None, role=None,
    )

    complete_mod.cmd_complete_agent(args)

    events = read_pending(spooling)
    assert events[0].args["thread_id"] == "resolved-uuid-1234"


def test_fail_agent_spools_and_never_opens_db(spooling, monkeypatch):
    monkeypatch.setattr(cli_common, "get_db", _blow_up_if_db_opened)
    args = Namespace(
        thread_id="AB", error="boom", failure_type="persistent",
        max_retries=2, recovery_dispatched=False,
    )

    complete_mod.cmd_fail_agent(args)

    events = read_pending(spooling)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "agent_fail"
    assert ev.args["thread_id"] == "AB"
    assert ev.args["error"] == "boom"
    assert ev.args["failure_type"] == "persistent"
    assert ev.args["max_retries"] == 2
    assert ev.args["recovery_dispatched"] is False


def test_fail_agent_resolves_thread_id_before_writing(spooling, monkeypatch):
    monkeypatch.setattr(cli_common, "get_db", _blow_up_if_db_opened)
    monkeypatch.setattr(cli_common, "resolve_thread_id_for_spool", lambda s: "resolved-uuid-5678")
    args = Namespace(
        thread_id="AB", error="boom", failure_type=None,
        max_retries=0, recovery_dispatched=False,
    )

    complete_mod.cmd_fail_agent(args)

    events = read_pending(spooling)
    assert events[0].args["thread_id"] == "resolved-uuid-5678"


def test_complete_agent_does_not_spool_when_should_spool_false(monkeypatch, tmp_path):
    """Sanity check: outside agent context, the normal DB path runs (and fails
    loudly on a missing thread) instead of silently spooling."""
    monkeypatch.setattr(cli_common, "_db_path", lambda: tmp_path / "j.db", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "j.db"))
    db.init_db()
    monkeypatch.setattr(cli_common, "get_db", lambda: db)
    args = Namespace(
        thread_id="ZZ", result_summary="ok", retain_text=None,
        open_questions=None, handoff=None, role=None,
    )

    with pytest.raises(SystemExit):
        complete_mod.cmd_complete_agent(args)
