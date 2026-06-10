"""Tests for per-agent tool-usage telemetry — the data layer that lets
`juggle agent-tools` right-size the deny block.

Covers: the agent_tool_events table + DB methods, the PreToolUse logging
helpers, the CLI report's deny cross-referencing, and that logging never raises
(telemetry must not break an agent's tool call).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "t.db"))
    d.init_db()
    return d


# --- DB layer -------------------------------------------------------------


def test_table_created_by_init(db):
    with db._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_tool_events)")}
    assert {
        "role",
        "tool_name",
        "mode",
        "count",
        "first_seen",
        "last_seen",
        "last_input",
    } <= cols


def test_record_increments_count_and_updates_last_input(db):
    db.record_agent_tool_use("coder", "Bash", "normal", "command=ls")
    db.record_agent_tool_use("coder", "Bash", "normal", "command=pwd")
    rows = db.get_agent_tool_usage("coder")
    assert len(rows) == 1
    assert rows[0]["count"] == 2
    assert rows[0]["last_input"] == "command=pwd"  # last write wins


def test_mode_distinguishes_rows(db):
    db.record_agent_tool_use("coder", "Edit", "normal")
    db.record_agent_tool_use("coder", "Edit", "audit")
    rows = db.get_agent_tool_usage("coder")
    assert {r["mode"] for r in rows} == {"normal", "audit"}
    assert all(r["count"] == 1 for r in rows)


def test_get_filters_by_role(db):
    db.record_agent_tool_use("coder", "Bash")
    db.record_agent_tool_use("researcher", "WebSearch")
    assert {r["tool_name"] for r in db.get_agent_tool_usage("coder")} == {"Bash"}
    assert len(db.get_agent_tool_usage()) == 2


def test_reset_clears_rows(db):
    db.record_agent_tool_use("coder", "Bash")
    assert db.reset_agent_tool_usage() == 1
    assert db.get_agent_tool_usage() == []


def test_record_safe_without_init(tmp_path):
    """Agents may hit a DB before init_db ran — the inline CREATE keeps it safe."""
    d = JuggleDB(str(tmp_path / "fresh.db"))
    d.record_agent_tool_use("coder", "Bash")
    assert d.get_agent_tool_usage("coder")[0]["count"] == 1


# --- PreToolUse logging helpers ------------------------------------------


def test_tool_input_sample_picks_telling_field():
    import juggle_hooks as h

    assert h._tool_input_sample({"command": "echo hi"}) == "command=echo hi"
    assert h._tool_input_sample({"old": "a", "file_path": "/x/y"}) == "file_path=/x/y"
    assert h._tool_input_sample({}) is None
    assert h._tool_input_sample({"weird": 1}) == "weird"  # falls back to key name
    long = "x" * 200
    assert h._tool_input_sample({"command": long}).endswith("...")


def test_log_agent_tool_use_writes_normal(tmp_path, monkeypatch):
    import juggle_hooks as h
    import juggle_hooks_config as cfg

    # Patch juggle_hooks_config.DB_PATH — _log_agent_tool_use reads _cfg.DB_PATH at call time.
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "h.db")
    monkeypatch.setenv("JUGGLE_AGENT_ROLE", "planner")
    monkeypatch.delenv("JUGGLE_AGENT_AUDIT", raising=False)
    h._log_agent_tool_use({"tool_name": "Read", "tool_input": {"file_path": "/a/b"}})
    rows = JuggleDB(str(tmp_path / "h.db")).get_agent_tool_usage("planner")
    assert rows[0]["tool_name"] == "Read"
    assert rows[0]["mode"] == "normal"
    assert rows[0]["last_input"] == "file_path=/a/b"


def test_log_agent_tool_use_tags_audit(tmp_path, monkeypatch):
    import juggle_hooks as h
    import juggle_hooks_config as cfg

    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "h2.db")
    monkeypatch.setenv("JUGGLE_AGENT_ROLE", "coder")
    monkeypatch.setenv("JUGGLE_AGENT_AUDIT", "1")
    h._log_agent_tool_use({"tool_name": "Edit", "tool_input": {}})
    assert JuggleDB(str(tmp_path / "h2.db")).get_agent_tool_usage("coder")[0]["mode"] == "audit"


def test_log_agent_tool_use_never_raises(tmp_path, monkeypatch):
    import juggle_hooks as h
    import juggle_hooks_config as cfg

    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "x.db")
    monkeypatch.setenv("JUGGLE_AGENT_ROLE", "coder")

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(h.JuggleDB, "record_agent_tool_use", boom)
    # Must swallow — telemetry failure cannot block the agent's tool call.
    h._log_agent_tool_use({"tool_name": "Bash", "tool_input": {}})


def test_log_agent_tool_use_skips_when_no_tool_name(tmp_path, monkeypatch):
    import juggle_hooks as h
    import juggle_hooks_config as cfg

    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "none.db")
    h._log_agent_tool_use({})  # no-op, no row
    assert JuggleDB(str(tmp_path / "none.db")).get_agent_tool_usage() == []


# --- CLI report -----------------------------------------------------------


def test_deny_matches_exact_and_wildcard():
    from juggle_cli import _deny_matches

    assert _deny_matches("mcp__github__create_pr", ["mcp__github__*"])
    assert not _deny_matches("mcp__gitlab__x", ["mcp__github__*"])
    assert _deny_matches("NotebookEdit", ["NotebookEdit"])
    assert not _deny_matches("Edit", ["NotebookEdit"])


def test_cli_report_flags_denied_but_used(tmp_path, capsys):
    import juggle_cli

    d = JuggleDB(str(tmp_path / "c.db"))
    d.init_db()
    # NotebookEdit is in coder's configured deny by default → flag when used.
    d.record_agent_tool_use("coder", "NotebookEdit", "audit")
    d.record_agent_tool_use("coder", "Bash", "normal")

    class Args:
        db_path = str(tmp_path / "c.db")
        role = None
        reset = False

    juggle_cli.cmd_agent_tools(Args())
    out = capsys.readouterr().out
    assert "NotebookEdit" in out
    assert "consider ALLOWING" in out
    assert "Bash" in out
