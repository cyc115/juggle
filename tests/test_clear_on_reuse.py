"""T-clear-on-reuse: reused pool agents must start with FRESH context.

In acquire_agent's reuse path, a '/clear' is sent to the pane BEFORE the
'cd <repo>' reset so the reused Claude Code agent drops its accumulated
transcript. Harness-gated: only for the 'claude' harness — other harnesses
(deepseek etc.) have no '/clear' and must be skipped.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "clear.db"))
    d.init_db()
    d.set_active(True)
    return d


@pytest.fixture
def thread_id(db):
    return db.create_thread("clear-test", session_id="")


def _fake_mgr(pane_id="%pool1"):
    mgr = MagicMock()
    mgr.wait_for_ready_to_paste.return_value = True
    mgr._run_tmux.return_value = MagicMock(returncode=0, stdout="")
    return mgr


def _clear_sends(mgr):
    """tmux send-keys calls whose payload is exactly '/clear'."""
    return [
        c for c in mgr._run_tmux.call_args_list
        if c.args[:1] == ("send-keys",) and "/clear" in c.args
    ]


def test_clear_issued_on_reuse_for_claude(db, thread_id, monkeypatch):
    """Reusing an idle claude agent sends '/clear' to the pane."""
    from juggle_dispatch_core import acquire_agent

    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")
    db.create_agent(role="coder", pane_id="%pool1", harness="claude", repo_path="")

    mgr = _fake_mgr()
    acquire_agent(db, thread_id, role="coder", harness="claude", _mgr=mgr)

    assert _clear_sends(mgr), "reuse of a claude agent must issue '/clear'"


def test_clear_before_cd_for_claude(db, thread_id, monkeypatch):
    """'/clear' must be sent BEFORE the 'cd <repo>' reset."""
    from juggle_dispatch_core import acquire_agent

    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")
    db.create_agent(role="coder", pane_id="%pool1", harness="claude", repo_path="")

    mgr = _fake_mgr()
    acquire_agent(db, thread_id, role="coder", harness="claude", _mgr=mgr)

    sends = [
        c.args for c in mgr._run_tmux.call_args_list
        if c.args[:1] == ("send-keys",)
    ]
    clear_idx = next(i for i, a in enumerate(sends) if "/clear" in a)
    cd_idx = next(i for i, a in enumerate(sends) if any("cd " in str(x) for x in a))
    assert clear_idx < cd_idx, "'/clear' must precede the 'cd' reset"


def test_clear_not_issued_for_non_claude(db, thread_id, monkeypatch):
    """Reusing a non-claude (deepseek) agent must NOT send '/clear'."""
    from juggle_dispatch_core import acquire_agent

    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")
    db.create_agent(role="coder", pane_id="%pool1", harness="deepseek", repo_path="")

    mgr = _fake_mgr()
    acquire_agent(db, thread_id, role="coder", harness="deepseek", _mgr=mgr)

    assert not _clear_sends(mgr), "non-claude reuse must NOT issue '/clear'"
