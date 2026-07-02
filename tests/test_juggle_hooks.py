"""Tests for juggle_hooks.py Stop handler and classification helpers."""

import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB
from juggle_hooks import get_classification_candidates, _bash_write_pattern
import juggle_hooks_config


@pytest.mark.parametrize(
    "cmd,expected_match",
    [
        ("sed -i 's/foo/bar/' file.py", True),
        ("sed --in-place 's/x/y/' f.txt", True),
        ("git add src/", True),
        ("git commit -m 'fix'", True),
        ("git push origin main", True),
        ("git reset --hard HEAD~1", True),
        ("rm -rf /tmp/old", True),
        ("rm file.txt", True),
        ("patch -p1 < changes.patch", True),
        ("tee output.log", True),
        ("mv old.py new.py", True),
        ("echo hello > out.txt", True),
        ("cat src.py >> dest.py", True),
        # Safe commands — must NOT be blocked
        ("git status", False),
        ("git log --oneline -10", False),
        ("git diff HEAD", False),
        ("grep -r foo src/", False),
        ("python3 juggle_cli.py start", False),
        ("curl -sf http://localhost:18888/health", False),
        ("cat file.py", False),
        ("ls -la", False),
        ("command 2> /dev/stderr", False),
        ("command > /dev/null", False),
        ("command 2>&1", False),
    ],
)
def test_bash_write_pattern(cmd, expected_match):
    result = _bash_write_pattern(cmd)
    if expected_match:
        assert result is not None, f"Expected block on: {cmd!r}"
    else:
        assert result is None, f"Expected pass on: {cmd!r} but got {result!r}"


@pytest.fixture
def active_db(tmp_path):
    # Use juggle.db so juggle_hooks.DB_PATH (_DATA_DIR / "juggle.db") resolves to the same file
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    db.set_active(True)
    tid = db.create_thread("Topic A", session_id="s1")
    db.set_current_thread(tid)
    return db


def test_stop_handler_captures_assistant_message(active_db, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active_db.db_path.parent))

    # Import after env is set so DB_PATH resolves correctly
    import importlib
    import juggle_hooks
    import juggle_hooks_config

    importlib.reload(juggle_hooks)
    monkeypatch.setattr(juggle_hooks_config, "DB_PATH", active_db.db_path)
    monkeypatch.setattr(juggle_hooks, "DB_PATH", active_db.db_path)

    data = {"last_assistant_message": "Here is my analysis of the auth flow."}

    with pytest.raises(SystemExit):
        juggle_hooks.handle_stop(data)

    messages = active_db.get_messages(active_db.get_current_thread(), token_budget=9999)
    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["content"] == "Here is my analysis of the auth flow."


def test_stop_handler_ignores_short_messages(active_db, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active_db.db_path.parent))
    import importlib
    import juggle_hooks

    importlib.reload(juggle_hooks)

    data = {"last_assistant_message": "ok"}  # too short

    with pytest.raises(SystemExit):
        juggle_hooks.handle_stop(data)

    messages = active_db.get_messages(active_db.get_current_thread(), token_budget=9999)
    assert not any(m["role"] == "assistant" for m in messages)


def test_stop_handler_falls_back_to_transcript_when_turn_ends_in_tool_call(
    active_db, monkeypatch, tmp_path
):
    """Regression pin (2026-07-01, fix-qa-capture-empty-answer): the
    orchestrator answers in prose, then ends its turn with a bare tool_use
    (e.g. ScheduleWakeup) — the harness's last_assistant_message field comes
    back empty because it only reflects the FINAL assistant record. Q&A
    history must still capture the earlier prose answer from the transcript."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active_db.db_path.parent))
    import importlib
    import juggle_hooks
    import juggle_hooks_config

    importlib.reload(juggle_hooks)
    monkeypatch.setattr(juggle_hooks_config, "DB_PATH", active_db.db_path)
    monkeypatch.setattr(juggle_hooks, "DB_PATH", active_db.db_path)

    transcript = tmp_path / "transcript.jsonl"
    records = [
        {"type": "user", "message": {"content": "What auth flow do we use?"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "We use OAuth2 with a rotating refresh token.",
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "ScheduleWakeup",
                        "input": {},
                    }
                ]
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in records))

    data = {"last_assistant_message": "", "transcript_path": str(transcript)}

    with pytest.raises(SystemExit):
        juggle_hooks.handle_stop(data)

    messages = active_db.get_messages(active_db.get_current_thread(), token_budget=9999)
    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["content"] == "We use OAuth2 with a rotating refresh token."


def test_stop_handler_missing_field_is_noop(active_db, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active_db.db_path.parent))
    import importlib
    import juggle_hooks

    importlib.reload(juggle_hooks)

    with pytest.raises(SystemExit):
        juggle_hooks.handle_stop({})  # no last_assistant_message key

    messages = active_db.get_messages(active_db.get_current_thread(), token_budget=9999)
    assert not any(m["role"] == "assistant" for m in messages)


# ---------------------------------------------------------------------------
# handle_pre_tool_use tests
# ---------------------------------------------------------------------------


def _reload_hooks(monkeypatch, active_db):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active_db.db_path.parent))
    import importlib
    import juggle_hooks
    import juggle_hooks_config

    importlib.reload(juggle_hooks)
    # Patch on the config module so all sub-modules reading _cfg.DB_PATH / _db_path() see it.
    monkeypatch.setattr(juggle_hooks_config, "DB_PATH", active_db.db_path)
    monkeypatch.setattr(juggle_hooks, "DB_PATH", active_db.db_path)
    return juggle_hooks


def test_pre_tool_use_blocks_edit_in_orchestrator(active_db, monkeypatch, capsys):
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    active_db.set_orchestrator_session_id("deadbeef1234")
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use(
            {"tool_name": "Edit", "session_id": "deadbeef1234"}
        )

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    payload = json.loads(stderr)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Edit" in payload["systemMessage"]


def test_pre_tool_use_blocks_write_in_orchestrator(active_db, monkeypatch):
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    active_db.set_orchestrator_session_id("abc")
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use({"tool_name": "Write", "session_id": "abc"})

    assert exc_info.value.code == 2


def test_pre_tool_use_allows_edit_in_agent_session(active_db, monkeypatch):
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use(
            {"tool_name": "Edit", "session_id": "agentpane"}
        )

    assert exc_info.value.code == 0


def test_pre_tool_use_allows_when_juggle_inactive(tmp_path, monkeypatch):
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    db.set_active(False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    import importlib
    import juggle_hooks
    import juggle_hooks_config

    importlib.reload(juggle_hooks)
    monkeypatch.setattr(juggle_hooks_config, "DB_PATH", db.db_path)
    monkeypatch.setattr(juggle_hooks, "DB_PATH", db.db_path)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use({"tool_name": "Edit", "session_id": "xyz"})

    assert exc_info.value.code == 0


def test_pre_tool_use_allows_non_blocked_tool(
    active_db, monkeypatch
):  # no capsys — exit 0, no output
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use({"tool_name": "Bash", "session_id": "xyz"})

    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# Agent-session context-injection guards (token saving)
# ---------------------------------------------------------------------------


def test_session_start_agent_injects_nothing(monkeypatch, capsys):
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    import juggle_hooks

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_session_start({"reason": "startup"})

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == ""


def test_user_prompt_submit_agent_anchor_only_no_dashboard(monkeypatch, capsys):
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    monkeypatch.setenv("JUGGLE_AGENT_ROLE", "coder")
    import juggle_hooks

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_user_prompt_submit(
            {"prompt": "[JUGGLE_THREAD:x] implement the widget"}
        )

    assert exc_info.value.code == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "AGENT ROLE" in ctx
    assert "ROLE: coder" in ctx
    # Orchestrator dashboard must NOT be present in an agent session.
    assert "JUGGLE ACTIVE" not in ctx


def test_post_tool_use_agent_no_violation_warning(monkeypatch, capsys):
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    import juggle_hooks

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_post_tool_use({"tool_name": "Read"})

    assert exc_info.value.code == 0
    # No "ORCHESTRATOR VIOLATION" warning injected for an agent's own reads.
    assert capsys.readouterr().out.strip() == ""


# ---------------------------------------------------------------------------
# Classification candidate filter tests
# ---------------------------------------------------------------------------


def _make_thread(tid: str, status: str) -> dict:
    # P8 Task 4.2: conversations carry node vocab — get_classification_candidates
    # filters on `state` (excludes done/archived).
    return {"thread_id": tid, "title": f"Topic {tid}", "state": status}


def test_classification_candidates_excludes_done():
    threads = [
        _make_thread("A", "active"),
        _make_thread("B", "done"),
        _make_thread("C", "background"),
    ]
    candidates = get_classification_candidates(threads)
    ids = [t["thread_id"] for t in candidates]
    assert "B" not in ids
    assert "A" in ids
    assert "C" in ids


def test_classification_candidates_excludes_archived():
    threads = [
        _make_thread("A", "active"),
        _make_thread("B", "archived"),
    ]
    candidates = get_classification_candidates(threads)
    ids = [t["thread_id"] for t in candidates]
    assert "B" not in ids
    assert "A" in ids


def test_classification_candidates_all_closed_returns_empty():
    threads = [
        _make_thread("A", "done"),
        _make_thread("B", "archived"),
    ]
    candidates = get_classification_candidates(threads)
    assert candidates == []


def test_classification_candidates_includes_all_open_statuses():
    threads = [
        _make_thread("A", "active"),
        _make_thread("B", "background"),
        _make_thread("C", "idle"),
        _make_thread("D", "waiting"),
    ]
    candidates = get_classification_candidates(threads)
    assert len(candidates) == 4


def test_classification_candidates_empty_input():
    assert get_classification_candidates([]) == []


# ---------------------------------------------------------------------------
# PostToolUse handler tests
# ---------------------------------------------------------------------------


def test_post_tool_use_forbidden_tool_warns(active_db, monkeypatch):
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active_db.db_path.parent))
    import importlib
    import io
    import sys as _sys
    import json
    import juggle_hooks
    import juggle_hooks_config

    importlib.reload(juggle_hooks)
    # Point is_active() at the isolated active DB (test isolation: must never
    # fall through to the production DB, which the conftest guard now blocks).
    monkeypatch.setattr(juggle_hooks_config, "DB_PATH", active_db.db_path)
    monkeypatch.setattr(juggle_hooks, "DB_PATH", active_db.db_path)

    data = {"tool_name": "Read", "tool_input": {}, "tool_response": ""}

    old_stdout = _sys.stdout
    _sys.stdout = io.StringIO()
    try:
        with pytest.raises(SystemExit):
            juggle_hooks.handle_post_tool_use(data)
        output = _sys.stdout.getvalue()
    finally:
        _sys.stdout = old_stdout

    parsed = json.loads(output)
    assert "ORCHESTRATOR VIOLATION" in parsed["hookSpecificOutput"]["additionalContext"]


def test_post_tool_use_no_agent_task_id_tracking(active_db, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active_db.db_path.parent))
    import importlib
    import juggle_hooks

    importlib.reload(juggle_hooks)

    thread_id = active_db.get_current_thread()
    prompt = f"[JUGGLE_THREAD:{thread_id}] Do some work."
    data = {
        "tool_name": "Agent",
        "tool_input": {"prompt": prompt, "run_in_background": True},
        "tool_response": '{"task_id": "abc-123"}',
    }

    with pytest.raises(SystemExit):
        juggle_hooks.handle_post_tool_use(data)

    thread = active_db.get_thread(thread_id)
    assert thread["state"] != "background"


# ---------------------------------------------------------------------------
# Autopilot enforcement tests (/juggle:toggle-autopilot flag is hook-read)
# ---------------------------------------------------------------------------


def test_autopilot_context_empty_when_flag_absent(active_db, monkeypatch, tmp_path):
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    import juggle_hooks_config
    monkeypatch.setattr(juggle_hooks_config, "AUTOPILOT_FLAG", tmp_path / "autopilot")
    monkeypatch.setattr(juggle_hooks, "AUTOPILOT_FLAG", tmp_path / "autopilot")
    assert juggle_hooks._autopilot_context() == ""


def test_autopilot_context_present_when_flag_set(active_db, monkeypatch, tmp_path):
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    import juggle_hooks_config
    flag = tmp_path / "autopilot"
    flag.touch()
    monkeypatch.setattr(juggle_hooks_config, "AUTOPILOT_FLAG", flag)
    monkeypatch.setattr(juggle_hooks, "AUTOPILOT_FLAG", flag)
    assert "AUTOPILOT MODE: ON" in juggle_hooks._autopilot_context()


def test_user_prompt_submit_injects_autopilot_when_active(
    active_db, monkeypatch, tmp_path, capsys
):
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    import juggle_hooks_config
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    flag = tmp_path / "autopilot"
    flag.touch()
    monkeypatch.setattr(juggle_hooks_config, "AUTOPILOT_FLAG", flag)
    monkeypatch.setattr(juggle_hooks, "AUTOPILOT_FLAG", flag)

    with pytest.raises(SystemExit):
        juggle_hooks.handle_user_prompt_submit({"prompt": ""})

    out = json.loads(capsys.readouterr().out)
    assert "AUTOPILOT MODE: ON" in out["hookSpecificOutput"]["additionalContext"]


def test_user_prompt_submit_injects_autopilot_when_inactive(
    active_db, monkeypatch, tmp_path, capsys
):
    active_db.set_active(False)
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    import juggle_hooks_config
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    flag = tmp_path / "autopilot"
    flag.touch()
    monkeypatch.setattr(juggle_hooks_config, "AUTOPILOT_FLAG", flag)
    monkeypatch.setattr(juggle_hooks, "AUTOPILOT_FLAG", flag)

    with pytest.raises(SystemExit):
        juggle_hooks.handle_user_prompt_submit({"prompt": ""})

    out = json.loads(capsys.readouterr().out)
    assert "AUTOPILOT MODE: ON" in out["hookSpecificOutput"]["additionalContext"]


def test_user_prompt_submit_no_autopilot_when_inactive_and_flag_absent(
    active_db, monkeypatch, tmp_path, capsys
):
    active_db.set_active(False)
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    import juggle_hooks_config
    monkeypatch.setattr(juggle_hooks_config, "AUTOPILOT_FLAG", tmp_path / "autopilot")
    monkeypatch.setattr(juggle_hooks, "AUTOPILOT_FLAG", tmp_path / "autopilot")
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    monkeypatch.setattr(juggle_hooks_config, "DB_PATH", active_db.db_path)
    monkeypatch.setattr(juggle_hooks, "DB_PATH", active_db.db_path)

    with pytest.raises(SystemExit):
        juggle_hooks.handle_user_prompt_submit({"prompt": ""})

    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# /tmp whitelist tests for PreToolUse
# ---------------------------------------------------------------------------


def test_pre_tool_use_write_to_tmp_is_allowed(active_db, monkeypatch):
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use(
            {
                "tool_name": "Write",
                "session_id": "abc",
                "tool_input": {"file_path": "/tmp/task.md"},
            }
        )

    assert exc_info.value.code == 0


def test_pre_tool_use_write_to_private_tmp_is_allowed(active_db, monkeypatch):
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use(
            {
                "tool_name": "Write",
                "session_id": "abc",
                "tool_input": {"file_path": "/private/tmp/task.md"},
            }
        )

    assert exc_info.value.code == 0


def test_pre_tool_use_write_to_repo_is_blocked(active_db, monkeypatch):
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    active_db.set_orchestrator_session_id("abc")
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use(
            {
                "tool_name": "Write",
                "session_id": "abc",
                "tool_input": {"file_path": "/Users/mikechen/github/juggle/src/foo.py"},
            }
        )

    assert exc_info.value.code == 2


def test_pre_tool_use_edit_to_tmp_is_allowed(active_db, monkeypatch):
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use(
            {
                "tool_name": "Edit",
                "session_id": "abc",
                "tool_input": {"file_path": "/tmp/task.md"},
            }
        )

    assert exc_info.value.code == 0


def test_pre_tool_use_edit_to_repo_is_blocked(active_db, monkeypatch):
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    active_db.set_orchestrator_session_id("abc")
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use(
            {
                "tool_name": "Edit",
                "session_id": "abc",
                "tool_input": {"file_path": "/Users/mikechen/github/juggle/src/foo.py"},
            }
        )

    assert exc_info.value.code == 2
