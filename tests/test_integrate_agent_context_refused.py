"""Tests for T-spool-10: integrate is watchdog-owned.

Regression scope: `juggle integrate <thread>` invoked directly from an
agent-context process (JUGGLE_IS_AGENT=1, e.g. a coder's own Terminal
Checklist step) must be refused — only the watchdog (JUGGLE_ORCHESTRATOR=1)
may run it. --allow-legacy-agent-integrate is the operator/legacy compat
bypass that restores today's unconditional behavior.
"""
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest


def _make_db():
    db = Mock()
    db.get_thread.return_value = {
        "id": "thread-uuid-1", "user_label": "AB",
        "worktree_path": "/tmp/wt", "worktree_branch": "cyc_AB",
        "main_repo_path": "/tmp/repo",
    }
    return db


def _args(**overrides):
    args = Mock()
    args.thread_id = "AB"
    args.allow_main = False
    args.allow_legacy_agent_integrate = False
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def test_cmd_integrate_refuses_in_agent_context(monkeypatch):
    """JUGGLE_IS_AGENT=1 (agent context, no orchestrator marker) → refused
    before _run_integrate is ever called."""
    from juggle_cmd_integrate import cmd_integrate

    monkeypatch.delenv("JUGGLE_ORCHESTRATOR", raising=False)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")

    db = _make_db()
    with patch("juggle_cmd_integrate.get_db", return_value=db):
        with patch("juggle_cmd_integrate._resolve_thread", return_value="thread-uuid-1"):
            with patch("juggle_cmd_integrate._run_integrate") as mock_run:
                with pytest.raises(SystemExit) as exc:
                    cmd_integrate(_args())
    assert exc.value.code == 1
    mock_run.assert_not_called()


def test_cmd_integrate_refusal_message_mentions_watchdog_owned(monkeypatch, capsys):
    from juggle_cmd_integrate import cmd_integrate

    monkeypatch.delenv("JUGGLE_ORCHESTRATOR", raising=False)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")

    db = _make_db()
    with patch("juggle_cmd_integrate.get_db", return_value=db):
        with patch("juggle_cmd_integrate._resolve_thread", return_value="thread-uuid-1"):
            with patch("juggle_cmd_integrate._run_integrate"):
                with pytest.raises(SystemExit):
                    cmd_integrate(_args())
    out = capsys.readouterr().out.lower()
    assert "watchdog" in out
    assert "--allow-legacy-agent-integrate" in out


def test_cmd_integrate_allowed_in_orchestrator_context(monkeypatch):
    """JUGGLE_ORCHESTRATOR=1 wins — the watchdog's own integrate call proceeds
    unrefused, exactly as it does today."""
    from juggle_cmd_integrate import cmd_integrate

    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")

    db = _make_db()
    with patch("juggle_cmd_integrate.get_db", return_value=db):
        with patch("juggle_cmd_integrate._resolve_thread", return_value="thread-uuid-1"):
            with patch("juggle_cmd_integrate._run_integrate", return_value=(True, "ok")) as mock_run:
                cmd_integrate(_args())  # should not raise SystemExit
    mock_run.assert_called_once()


def test_cmd_integrate_allow_legacy_flag_bypasses_refusal_in_agent_context(monkeypatch):
    """--allow-legacy-agent-integrate restores unconditional legacy behavior
    even under JUGGLE_IS_AGENT=1."""
    from juggle_cmd_integrate import cmd_integrate

    monkeypatch.delenv("JUGGLE_ORCHESTRATOR", raising=False)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")

    db = _make_db()
    with patch("juggle_cmd_integrate.get_db", return_value=db):
        with patch("juggle_cmd_integrate._resolve_thread", return_value="thread-uuid-1"):
            with patch("juggle_cmd_integrate._run_integrate", return_value=(True, "ok")) as mock_run:
                cmd_integrate(_args(allow_legacy_agent_integrate=True))
    mock_run.assert_called_once()


def test_cmd_integrate_allowed_when_no_agent_context_signals(monkeypatch, tmp_path):
    """Plain operator shell (no JUGGLE_IS_AGENT, no JUGGLE_ORCHESTRATOR, cwd
    outside a juggle-juggle-* worktree) is not agent context — proceeds
    unrefused."""
    from juggle_cmd_integrate import cmd_integrate

    monkeypatch.delenv("JUGGLE_ORCHESTRATOR", raising=False)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    monkeypatch.delenv("JUGGLE_AGENT_WORKTREE", raising=False)
    monkeypatch.chdir(tmp_path)  # dev suite itself may run from a juggle-juggle-* worktree

    db = _make_db()
    with patch("juggle_cmd_integrate.get_db", return_value=db):
        with patch("juggle_cmd_integrate._resolve_thread", return_value="thread-uuid-1"):
            with patch("juggle_cmd_integrate._run_integrate", return_value=(True, "ok")) as mock_run:
                cmd_integrate(_args())
    mock_run.assert_called_once()
