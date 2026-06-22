"""Regression pins for the agent verify-loop CODE backstop (TODO L13).

Incident 2026-06-21: agents that ignore the coder-template "verify ONCE"
directive still zombie-loop — spawn the full suite as a BACKGROUND job, poll it,
"come to rest", repeat — burning 100k–330k tokens each (4 agents did this). The
bc514f3 fix was prompt-only (template) + a `juggle verify` helper; neither can
stop a template-ignoring agent. This is the deferred (a48433ac DA M1/I2) HARNESS
backstop: a hard per-agent cap on repeated BACKGROUND full-suite/verify spawns,
enforced in the PreToolUse hook. A single FOREGROUND `juggle verify` (the
sanctioned path) is never counted, so it is never blocked.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB  # noqa: E402
from juggle_verify_cap import (  # noqa: E402
    MAX_BG_VERIFY_SPAWNS,
    is_bg_suite_spawn,
)
from dbops.verify_spawns import bump_verify_spawn  # noqa: E402


# ── pure detector: only BACKGROUND suite/verify spawns count ────────────────


@pytest.mark.parametrize(
    "tool_name,tool_input,expected",
    [
        # Background full-suite / verify spawns → counted
        ("Bash", {"command": "uv run pytest -q", "run_in_background": True}, True),
        ("Bash", {"command": "juggle verify", "run_in_background": True}, True),
        (
            "Bash",
            {"command": "uv run src/juggle_cli.py verify", "run_in_background": True},
            True,
        ),
        ("Bash", {"command": "make test", "run_in_background": True}, True),
        ("Bash", {"command": "python -m pytest tests/", "run_in_background": True}, True),
        # FOREGROUND verify — the sanctioned single run — must NEVER count
        ("Bash", {"command": "juggle verify"}, False),
        ("Bash", {"command": "uv run pytest -q", "run_in_background": False}, False),
        # Background non-suite command → not counted
        ("Bash", {"command": "ls -la", "run_in_background": True}, False),
        ("Bash", {"command": "git status", "run_in_background": True}, False),
        # Non-Bash tools never count
        ("Read", {"file_path": "/x/pytest_notes.py"}, False),
        ("Bash", {}, False),
    ],
)
def test_is_bg_suite_spawn(tool_name, tool_input, expected):
    assert is_bg_suite_spawn(tool_name, tool_input) is expected


# ── per-agent-session counter ───────────────────────────────────────────────


def test_bump_verify_spawn_increments_per_session(tmp_path):
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    dbp = db.db_path
    assert bump_verify_spawn(dbp, "agent-A") == 1
    assert bump_verify_spawn(dbp, "agent-A") == 2
    assert bump_verify_spawn(dbp, "agent-A") == 3


def test_bump_verify_spawn_isolated_across_sessions(tmp_path):
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    dbp = db.db_path
    assert bump_verify_spawn(dbp, "agent-A") == 1
    assert bump_verify_spawn(dbp, "agent-B") == 1  # independent counter
    assert bump_verify_spawn(dbp, "agent-A") == 2


# ── hook-level enforcement (the actual backstop) ────────────────────────────


@pytest.fixture
def active_db(tmp_path):
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    db.set_active(True)
    return db


def _reload_hooks(monkeypatch, active_db):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active_db.db_path.parent))
    import importlib
    import juggle_hooks
    import juggle_hooks_config

    importlib.reload(juggle_hooks)
    monkeypatch.setattr(juggle_hooks_config, "DB_PATH", active_db.db_path)
    monkeypatch.setattr(juggle_hooks, "DB_PATH", active_db.db_path)
    return juggle_hooks


def _bg_verify(session_id):
    return {
        "tool_name": "Bash",
        "session_id": session_id,
        "tool_input": {"command": "juggle verify", "run_in_background": True},
    }


def test_bg_verify_under_cap_is_allowed(active_db, monkeypatch):
    """The first MAX_BG_VERIFY_SPAWNS background spawns pass (exit 0)."""
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")

    for _ in range(MAX_BG_VERIFY_SPAWNS):
        with pytest.raises(SystemExit) as exc_info:
            juggle_hooks.handle_pre_tool_use(_bg_verify("agent-loop"))
        assert exc_info.value.code == 0


def test_bg_verify_over_cap_is_denied(active_db, monkeypatch, capsys):
    """The spawn AFTER the cap is hard-denied (exit 2 + deny payload)."""
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")

    for _ in range(MAX_BG_VERIFY_SPAWNS):
        with pytest.raises(SystemExit):
            juggle_hooks.handle_pre_tool_use(_bg_verify("agent-loop"))
    capsys.readouterr()  # drain

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use(_bg_verify("agent-loop"))
    assert exc_info.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "verify" in payload["systemMessage"].lower()


def test_foreground_verify_never_blocked(active_db, monkeypatch):
    """A FOREGROUND `juggle verify` is the sanctioned path — never capped."""
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")

    fg = {
        "tool_name": "Bash",
        "session_id": "agent-fg",
        "tool_input": {"command": "juggle verify"},  # foreground
    }
    for _ in range(MAX_BG_VERIFY_SPAWNS + 5):
        with pytest.raises(SystemExit) as exc_info:
            juggle_hooks.handle_pre_tool_use(fg)
        assert exc_info.value.code == 0
