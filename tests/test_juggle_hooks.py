"""Tests for juggle_hooks.py Stop handler and classification helpers."""
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB
from juggle_hooks import get_classification_candidates, _bash_write_pattern


@pytest.mark.parametrize("cmd,expected_match", [
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
])
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
    importlib.reload(juggle_hooks)

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
    importlib.reload(juggle_hooks)
    return juggle_hooks


def test_pre_tool_use_blocks_edit_in_orchestrator(active_db, monkeypatch, capsys):
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use({"tool_name": "Edit", "session_id": "deadbeef1234"})

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    payload = json.loads(stderr)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Edit" in payload["systemMessage"]


def test_pre_tool_use_blocks_write_in_orchestrator(active_db, monkeypatch):
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use({"tool_name": "Write", "session_id": "abc"})

    assert exc_info.value.code == 2


def test_pre_tool_use_allows_edit_in_agent_session(active_db, monkeypatch):
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use({"tool_name": "Edit", "session_id": "agentpane"})

    assert exc_info.value.code == 0


def test_pre_tool_use_allows_when_juggle_inactive(tmp_path, monkeypatch):
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    db.set_active(False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    import importlib
    import juggle_hooks
    importlib.reload(juggle_hooks)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use({"tool_name": "Edit", "session_id": "xyz"})

    assert exc_info.value.code == 0


def test_pre_tool_use_allows_non_blocked_tool(active_db, monkeypatch):  # no capsys — exit 0, no output
    juggle_hooks = _reload_hooks(monkeypatch, active_db)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_pre_tool_use({"tool_name": "Bash", "session_id": "xyz"})

    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# Classification candidate filter tests
# ---------------------------------------------------------------------------

def _make_thread(tid: str, status: str) -> dict:
    return {"thread_id": tid, "topic": f"Topic {tid}", "status": status}


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
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active_db.db_path.parent))
    import importlib
    import io
    import sys as _sys
    import json
    import juggle_hooks
    importlib.reload(juggle_hooks)

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
    assert thread["status"] != "background"
