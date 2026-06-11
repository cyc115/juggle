"""UserPromptSubmit hook must fail-open on filesystem/OSError — never block a prompt.

Regression: 2026-06-11 — self-referential .venv symlink (OSError ELOOP errno 62)
caused the UserPromptSubmit hook to exit non-zero, blocking all agent prompt
submissions. The hook's job is additive (inject context); a filesystem error must
never prevent the user/agent prompt from proceeding.
"""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB
import juggle_hooks_config

_ELOOP = OSError(62, "Too many levels of symbolic links")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def active_db(tmp_path):
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    db.set_active(True)
    tid = db.create_thread("Test Topic", session_id="s1")
    db.set_current_thread(tid)
    return db


def _reload(monkeypatch, active_db):
    """Reload hook modules pointing at the test DB; return (juggle_hooks, juggle_hooks_prompt)."""
    import importlib

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active_db.db_path.parent))
    import juggle_hooks
    import juggle_hooks_prompt

    importlib.reload(juggle_hooks)
    monkeypatch.setattr(juggle_hooks_config, "DB_PATH", active_db.db_path)
    monkeypatch.setattr(juggle_hooks, "DB_PATH", active_db.db_path)
    return juggle_hooks, juggle_hooks_prompt


# ---------------------------------------------------------------------------
# UserPromptSubmit fail-open tests
# ---------------------------------------------------------------------------


def test_user_prompt_submit_os_error_exits_zero_warns_stderr(active_db, monkeypatch, capsys):
    """OSError during context-gathering must exit 0 (fail-open) AND write to stderr.

    Regression: 2026-06-11 — ELOOP on .venv symlink caused exit non-zero, blocking
    all agent prompts because the hook's context-building raised OSError(62).
    """
    juggle_hooks, juggle_hooks_prompt = _reload(monkeypatch, active_db)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)

    def _raise(db_path=None):
        raise _ELOOP

    monkeypatch.setattr(juggle_hooks_prompt, "build_context_string", _raise)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_user_prompt_submit({"prompt": "do something"})

    assert exc_info.value.code == 0, "Must not block prompt on OSError"
    stderr = capsys.readouterr().err
    assert "[juggle] WARNING" in stderr, "Must emit a diagnosable warning to stderr"


def test_user_prompt_submit_agent_os_error_exits_zero_warns_stderr(monkeypatch, capsys):
    """Agent path: OSError during anchor-building must exit 0 with stderr warning.

    Regression: 2026-06-11 — same ELOOP issue affected the JUGGLE_IS_AGENT=1 path.
    """
    import importlib

    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    monkeypatch.setenv("JUGGLE_AGENT_ROLE", "coder")
    import juggle_hooks
    import juggle_hooks_prompt

    importlib.reload(juggle_hooks)

    def _raise(db_path=None):
        raise _ELOOP

    monkeypatch.setattr(juggle_hooks_prompt, "build_context_string", _raise)

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_user_prompt_submit({"prompt": "agent task"})

    assert exc_info.value.code == 0, "Agent path must not block on OSError"
    stderr = capsys.readouterr().err
    assert "[juggle] WARNING" in stderr, "Must emit a diagnosable warning to stderr"


def test_user_prompt_submit_happy_path_still_injects_context(active_db, monkeypatch, capsys):
    """Happy path: normal filesystem still injects context after fail-open hardening."""
    juggle_hooks, _ = _reload(monkeypatch, active_db)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    monkeypatch.setattr(
        juggle_hooks_config,
        "AUTOPILOT_FLAG",
        active_db.db_path.parent / "no-autopilot-here",
    )

    with pytest.raises(SystemExit) as exc_info:
        juggle_hooks.handle_user_prompt_submit({"prompt": "hello"})

    assert exc_info.value.code == 0
    out = capsys.readouterr().out.strip()
    if out:
        payload = json.loads(out)
        assert "JUGGLE" in payload["hookSpecificOutput"]["additionalContext"]


def test_main_dispatcher_fails_open_on_user_prompt_submit_os_error(monkeypatch, capsys):
    """Safety belt: main() must exit 0 (not 1) for UserPromptSubmit even if the handler raises.

    Regression: 2026-06-11 — if OSError escapes the handler (e.g. import-time failure),
    main()'s catch-all previously called sys.exit(1), blocking the prompt.
    """
    import importlib

    import juggle_hooks

    importlib.reload(juggle_hooks)

    def _explode(data):
        raise OSError(62, "Too many levels of symbolic links")

    monkeypatch.setattr(
        juggle_hooks,
        "HANDLERS",
        {**juggle_hooks.HANDLERS, "UserPromptSubmit": _explode},
    )

    old_stdin = sys.stdin
    old_argv = sys.argv
    sys.stdin = io.StringIO("{}")
    sys.argv = ["juggle_hooks.py", "UserPromptSubmit"]
    try:
        with pytest.raises(SystemExit) as exc_info:
            juggle_hooks.main()
    finally:
        sys.stdin = old_stdin
        sys.argv = old_argv

    assert exc_info.value.code == 0, (
        "main() must exit 0 for UserPromptSubmit even on unhandled OSError"
    )
    stderr = capsys.readouterr().err
    assert "[juggle] WARNING" in stderr


def test_render_agent_role_anchor_path_resolve_os_error_returns_fallback(monkeypatch):
    """render_agent_role_anchor_for must not raise when Path.resolve() fails with ELOOP.

    The completion-line path fallback must be used instead of blocking.
    """
    import pathlib

    original_resolve = pathlib.Path.resolve  # noqa: F841

    def _resolve_raises(self, *args, **kwargs):
        raise OSError(62, "Too many levels of symbolic links")

    monkeypatch.setattr(pathlib.Path, "resolve", _resolve_raises)

    monkeypatch.setenv("JUGGLE_AGENT_ROLE", "coder")
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

    import importlib
    import juggle_context

    importlib.reload(juggle_context)

    # Must not raise; must return a string (may be "" if role has no context configured)
    result = juggle_context.render_agent_role_anchor_for("coder")
    assert isinstance(result, str)
