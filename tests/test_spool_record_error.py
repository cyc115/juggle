"""Task 6 (spool-single-writer): juggle_selfheal.record_error spools in agent
context instead of opening the DB — closing the last failure-path migration-guard
hazard (a Class A error recorded from a worktree/agent used to write the shared
prod DB directly, tripping the stale-schema/migration guard).

In agent context (should_spool() True) record_error writes a single 'record_error'
spool event carrying every field the drain needs to replay dedup_or_insert_error,
and never opens a DB connection. Outside agent context it keeps writing directly.
"""
import json
from argparse import Namespace  # noqa: F401 — parity with sibling spool tests

import pytest

import juggle_selfheal as sh
from dbops.spool import read_pending


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    # Neutralize the ambient agent context (this suite may itself run inside a
    # dispatched agent / juggle-juggle-* worktree) so each test sets it explicitly.
    for var in ("JUGGLE_IS_AGENT", "JUGGLE_ORCHESTRATOR", "JUGGLE_AGENT_WORKTREE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("JUGGLE_SELFHEAL_OP", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def spooling(monkeypatch, tmp_path):
    """should_spool() → True and spool_dir() resolves under tmp_path."""
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    monkeypatch.setenv("JUGGLE_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path / "cfg" / "spool"


def _blow_up_if_db_opened(*_a, **_k):
    raise AssertionError("DB must not be opened on the spool early-return path")


def _raise():
    raise ValueError("kaboom")


def test_record_error_spools_and_never_opens_db(spooling, monkeypatch):
    monkeypatch.setattr(sh, "_get_db", _blow_up_if_db_opened)
    try:
        _raise()
    except ValueError as exc:
        sh.record_error(exc, "juggle_hooks.UserPromptSubmit", {"argv": ["x"]})

    events = read_pending(spooling)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "record_error"
    assert ev.args["error_class"] == "A"
    assert ev.args["exc_type"] == "ValueError"
    assert ev.args["entrypoint"] == "juggle_hooks.UserPromptSubmit"
    assert "kaboom" in ev.args["traceback"]
    assert json.loads(ev.args["command_args"]) == {"argv": ["x"]}
    assert ev.args["signature_hash"]  # non-empty class-A signature


def test_record_error_allowlisted_still_skipped_in_agent_context(spooling, monkeypatch):
    """Allowlisted exceptions (SystemExit/KeyboardInterrupt/'database is locked')
    are dropped BEFORE the spool decision — they never produce a spool event."""
    monkeypatch.setattr(sh, "_get_db", _blow_up_if_db_opened)
    sh.record_error(KeyboardInterrupt(), "juggle_cli.main")
    assert read_pending(spooling) == []


def test_record_error_reentrancy_guard_suppresses_spool(spooling, monkeypatch):
    """The _SELFHEAL_ENV re-entrancy guard short-circuits before spooling too."""
    monkeypatch.setattr(sh, "_get_db", _blow_up_if_db_opened)
    monkeypatch.setenv("JUGGLE_SELFHEAL_OP", "1")
    try:
        _raise()
    except ValueError as exc:
        sh.record_error(exc, "juggle_cli.main")
    assert read_pending(spooling) == []


def test_record_error_writes_db_when_not_agent_context(monkeypatch, tmp_path):
    """Outside agent context record_error still writes the row directly (no spool)."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "j.db"))
    db.init_db()
    monkeypatch.setattr(sh, "_get_db", lambda: db)
    monkeypatch.setenv("JUGGLE_CONFIG_DIR", str(tmp_path / "cfg"))

    try:
        _raise()
    except ValueError as exc:
        sh.record_error(exc, "juggle_cli.main")

    with db._connect() as conn:
        rows = conn.execute("SELECT exc_type, entrypoint FROM error_events").fetchall()
    assert len(rows) == 1
    assert rows[0]["exc_type"] == "ValueError"
    # And nothing was spooled.
    assert read_pending(tmp_path / "cfg" / "spool") == []


# ---- Task 6b: record_orchestration_error (Class B) mirrors record_error ----


def test_record_orchestration_error_spools_and_never_opens_db(spooling, monkeypatch):
    monkeypatch.setattr(sh, "_get_db", _blow_up_if_db_opened)
    sh.record_orchestration_error(
        tool="Bash",
        tool_input={"command": "ls"},
        error_text="exit 1: no such file",
        juggle_ref="juggle_cmd_agents.py:42",
    )

    events = read_pending(spooling)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "record_orchestration_error"
    assert ev.args["error_class"] == "B"
    assert ev.args["exc_type"] is None
    assert ev.args["entrypoint"] == "Bash"
    assert ev.args["traceback"] == "exit 1: no such file"
    assert json.loads(ev.args["command_args"]) == {"command": "ls"}
    assert ev.args["surface"] == "juggle_cmd_agents.py:42"
    assert ev.args["juggle_ref"] == "juggle_cmd_agents.py:42"
    assert ev.args["signature_hash"]  # non-empty class-B signature


def test_record_orchestration_error_reentrancy_guard_suppresses_spool(spooling, monkeypatch):
    monkeypatch.setattr(sh, "_get_db", _blow_up_if_db_opened)
    monkeypatch.setenv("JUGGLE_SELFHEAL_OP", "1")
    sh.record_orchestration_error("Bash", {"command": "ls"}, "boom", "ref:1")
    assert read_pending(spooling) == []


def test_record_orchestration_error_writes_db_when_not_agent_context(monkeypatch, tmp_path):
    """Outside agent context it still writes the row directly (no spool)."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "j.db"))
    db.init_db()
    monkeypatch.setattr(sh, "_get_db", lambda: db)
    monkeypatch.setenv("JUGGLE_CONFIG_DIR", str(tmp_path / "cfg"))

    sh.record_orchestration_error("Bash", {"command": "ls"}, "boom", "ref:7")

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT error_class, entrypoint, juggle_ref FROM error_events"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["error_class"] == "B"
    assert rows[0]["entrypoint"] == "Bash"
    assert rows[0]["juggle_ref"] == "ref:7"
    assert read_pending(tmp_path / "cfg" / "spool") == []


def test_drain_applies_record_orchestration_error(monkeypatch, tmp_path):
    """The watchdog-side drain replays a record_orchestration_error event through
    dedup_or_insert_error with the Class B surface/juggle_ref fields intact."""
    from juggle_db import JuggleDB
    from juggle_spool_apply import drain_spool
    from dbops.spool import write_event

    db = JuggleDB(str(tmp_path / "j.db"))
    db.init_db()
    monkeypatch.setenv("JUGGLE_CONFIG_DIR", str(tmp_path / "cfg"))
    spool = tmp_path / "cfg" / "spool"
    write_event(spool, "record_orchestration_error", "", "", {
        "signature_hash": "sig-b", "error_class": "B", "exc_type": None,
        "traceback": "tool error", "entrypoint": "Bash",
        "command_args": '{"command": "ls"}',
        "surface": "juggle_cmd_agents.py:9", "juggle_ref": "juggle_cmd_agents.py:9",
    })
    stats = drain_spool(db)
    assert stats["applied"] == 1
    with db._connect() as conn:
        row = conn.execute(
            "SELECT error_class, entrypoint, juggle_ref FROM error_events "
            "WHERE signature_hash=?", ("sig-b",)
        ).fetchone()
    assert row is not None
    assert row["error_class"] == "B"
    assert row["juggle_ref"] == "juggle_cmd_agents.py:9"
