"""Regression tests for orchestrator session-scoping in PreToolUse hook.

Bug (2026-06-07): handle_pre_tool_use blocked Write/Edit in ANY active juggle
session, not just the orchestrator. Non-orchestrator sessions like [db302a7b]
that never ran /juggle:start were incorrectly blocked.

Root cause: is_active() checks a global DB flag with no session-ID comparison.

Fix: record orchestrator_session_id in DB when /juggle:start runs; only block
when current session_id matches, with 24-hour TTL for stale-session expiry.
"""
import importlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ORCH_SESSION = "orch-session-abcdef1234567890"
OTHER_SESSION = "other-session-db302a7b99999999"
TTL_SECS = 86400  # 24 h


def _make_active_db(tmp_path) -> JuggleDB:
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    db.set_active(True)
    tid = db.create_thread("TestTopic", session_id="s1")
    db.set_current_thread(tid)
    return db


def _reload_hooks(monkeypatch, db: JuggleDB):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(db.db_path.parent))
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    import juggle_hooks
    import juggle_hooks_config
    importlib.reload(juggle_hooks)
    # Patch juggle_hooks_config — sub-modules read DB_PATH via _cfg.DB_PATH at call time.
    monkeypatch.setattr(juggle_hooks_config, "DB_PATH", db.db_path)
    monkeypatch.setattr(juggle_hooks, "DB_PATH", db.db_path)
    return juggle_hooks


# ---------------------------------------------------------------------------
# DB helper tests
# ---------------------------------------------------------------------------

def test_db_set_get_orchestrator_session_id(tmp_path):
    """JuggleDB must expose get/set for orchestrator_session_id."""
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    # Initially empty
    assert db.get_orchestrator_session_id() == ""

    db.set_orchestrator_session_id(ORCH_SESSION)
    assert db.get_orchestrator_session_id() == ORCH_SESSION

    # Clear on stop
    db.set_orchestrator_session_id("")
    assert db.get_orchestrator_session_id() == ""


def test_db_orchestrator_session_ts_set_and_read(tmp_path):
    """set_orchestrator_session_id must also record a timestamp."""
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    before = time.time()
    db.set_orchestrator_session_id(ORCH_SESSION)
    after = time.time()

    ts = db.get_orchestrator_session_ts()
    assert before <= ts <= after


# ---------------------------------------------------------------------------
# PreToolUse: non-orchestrator session must be ALLOWED
# ---------------------------------------------------------------------------

def test_pre_tool_use_allows_non_orchestrator_session(tmp_path, monkeypatch, capsys):
    """A session that did NOT run /juggle:start must NOT be blocked, even when
    juggle is globally active.

    Currently FAILS: is_active() returns True for all sessions → blocks all.
    """
    db = _make_active_db(tmp_path)
    db.set_orchestrator_session_id(ORCH_SESSION)

    hooks = _reload_hooks(monkeypatch, db)

    # OTHER_SESSION is NOT the registered orchestrator
    with pytest.raises(SystemExit) as exc_info:
        hooks.handle_pre_tool_use({
            "tool_name": "Write",
            "session_id": OTHER_SESSION,
            "tool_input": {"file_path": "/Users/mikechen/notes/something.md"},
        })

    assert exc_info.value.code == 0, (
        f"Non-orchestrator session {OTHER_SESSION!r} must be ALLOWED (exit 0). "
        f"Got exit {exc_info.value.code}. "
        f"Bug: handle_pre_tool_use checks is_active() only, not session_id match."
    )
    # Must NOT produce a deny payload
    stderr = capsys.readouterr().err
    assert "deny" not in stderr.lower()


def test_pre_tool_use_allows_edit_in_non_orchestrator_session(tmp_path, monkeypatch, capsys):
    """Edit tool in a non-orchestrator session must also be allowed."""
    db = _make_active_db(tmp_path)
    db.set_orchestrator_session_id(ORCH_SESSION)

    hooks = _reload_hooks(monkeypatch, db)

    with pytest.raises(SystemExit) as exc_info:
        hooks.handle_pre_tool_use({
            "tool_name": "Edit",
            "session_id": OTHER_SESSION,
            "tool_input": {"file_path": "/Users/mikechen/notes/file.py"},
        })

    assert exc_info.value.code == 0, (
        "Edit in non-orchestrator session must be ALLOWED."
    )


# ---------------------------------------------------------------------------
# PreToolUse: genuine orchestrator session must still be BLOCKED
# ---------------------------------------------------------------------------

def test_pre_tool_use_blocks_genuine_orchestrator_session(tmp_path, monkeypatch, capsys):
    """The session that ran /juggle:start must still be blocked from Write/Edit.

    Regression guard: the fix must not break the original orchestrator guard.
    """
    db = _make_active_db(tmp_path)
    db.set_orchestrator_session_id(ORCH_SESSION)

    hooks = _reload_hooks(monkeypatch, db)

    with pytest.raises(SystemExit) as exc_info:
        hooks.handle_pre_tool_use({
            "tool_name": "Write",
            "session_id": ORCH_SESSION,
            "tool_input": {"file_path": "/Users/mikechen/notes/file.md"},
        })

    assert exc_info.value.code == 2, (
        "Orchestrator session must be DENIED (exit 2)."
    )
    stderr = capsys.readouterr().err
    payload = json.loads(stderr)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# PreToolUse: no orchestrator_session_id stored → ALLOW (safe default)
# ---------------------------------------------------------------------------

def test_pre_tool_use_allows_when_no_orchestrator_session_recorded(
    tmp_path, monkeypatch, capsys
):
    """When juggle is active but orchestrator_session_id was never set (old DB
    or pre-fix state), no session should be blocked.

    Currently FAILS: any active session is blocked.
    """
    db = _make_active_db(tmp_path)
    # Do NOT call set_orchestrator_session_id — simulates old/unset state

    hooks = _reload_hooks(monkeypatch, db)

    with pytest.raises(SystemExit) as exc_info:
        hooks.handle_pre_tool_use({
            "tool_name": "Write",
            "session_id": "any-session-id-here",
            "tool_input": {"file_path": "/Users/mikechen/notes/file.md"},
        })

    assert exc_info.value.code == 0, (
        "When no orchestrator session is recorded, no session should be blocked. "
        f"Got exit {exc_info.value.code}."
    )


# ---------------------------------------------------------------------------
# PreToolUse: stale orchestrator session (TTL exceeded) → ALLOW
# ---------------------------------------------------------------------------

def test_pre_tool_use_allows_stale_orchestrator_session(tmp_path, monkeypatch, capsys):
    """When the orchestrator session is older than TTL (24h), its edits should
    be allowed — the stale session can no longer block the user.

    Currently FAILS: no TTL check exists.
    """
    db = _make_active_db(tmp_path)
    db.set_orchestrator_session_id(ORCH_SESSION)

    # Backdate the session timestamp to 25 hours ago (beyond 24h TTL)
    stale_ts = time.time() - (TTL_SECS + 3600)
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO session(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("orchestrator_session_ts", str(stale_ts)),
        )
        conn.commit()

    hooks = _reload_hooks(monkeypatch, db)

    with pytest.raises(SystemExit) as exc_info:
        hooks.handle_pre_tool_use({
            "tool_name": "Write",
            "session_id": ORCH_SESSION,  # Even the orchestrator session is allowed when stale
            "tool_input": {"file_path": "/Users/mikechen/notes/file.md"},
        })

    assert exc_info.value.code == 0, (
        f"Stale orchestrator session (>24h old) must be ALLOWED (exit 0). "
        f"Got exit {exc_info.value.code}."
    )


# ---------------------------------------------------------------------------
# cmd_start: must record orchestrator_session_id from env
# ---------------------------------------------------------------------------

def test_cmd_start_stores_orchestrator_session_id(tmp_path, monkeypatch):
    """cmd_start() must record CLAUDE_CODE_SESSION_ID as orchestrator_session_id.

    Currently FAILS: cmd_start() calls set_active(True) but never sets
    orchestrator_session_id.
    """
    import juggle_cmd_threads
    import juggle_settings

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ORCH_SESSION)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setattr(juggle_cmd_threads, "_DATA_DIR", tmp_path)

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    monkeypatch.setattr(juggle_cmd_threads, "get_db", lambda: db)

    # Prevent side effects from watchdog/talkback
    monkeypatch.setattr(juggle_cmd_threads, "_start_watchdog", lambda: None)
    monkeypatch.setattr(juggle_cmd_threads, "_maybe_start_talkback", lambda: None)

    juggle_cmd_threads.cmd_start(None)

    stored = db.get_orchestrator_session_id()
    assert stored == ORCH_SESSION, (
        f"cmd_start must store CLAUDE_CODE_SESSION_ID as orchestrator_session_id. "
        f"Got: {stored!r}"
    )
