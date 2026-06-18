"""Regression: autopilot dispatch must bind agents/threads to the CANONICAL
source repo, never the orchestrator's launch cwd.

Incident (2026-06-16, multi-repo mis-binding cascade): when the orchestrator was
launched from the plugin install dir (~/.claude — itself a git repo), agent
spawn (``JuggleTmuxManager.spawn_agent``) and get-agent reuse-filtering both
resolved the repo via ``git rev-parse --show-toplevel`` against ``os.getcwd()``,
yielding ``/Users/mikechen/.claude``. That became ``agent.repo_path`` →
``thread.main_repo_path`` → worktrees at ``/tmp/juggle-.claude-<topic>`` and
``integrate`` ran against the WRONG repo (empty topic branch → work dropped, or
a stray ff-merge that pushed a partial commit set to origin/main out-of-band).

Root cause: cwd-derived repo binding. Fix: resolve from ``canonical_repo_path()``
(``git worktree list`` anchored on juggle's own ``__file__``), independent of cwd.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
import juggle_tmux as jt  # noqa: E402
import juggle_watchdog_singleton as ws  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "binding.db"))
    d.init_db()
    return d


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_spawn_agent_repo_is_canonical_not_cwd(db, tmp_path, monkeypatch):
    """spawn_agent must tag agent.repo_path with canonical_repo_path(), NOT the
    orchestrator's cwd git toplevel.

    Simulates the incident: cwd is a DIFFERENT git repo (the ~/.claude plugin
    install dir stand-in) while canonical_repo_path() points at the real source.
    """
    wrong_repo = tmp_path / "dot-claude"   # what os.getcwd() toplevel returns
    src_repo = tmp_path / "github-juggle"  # what binding MUST resolve to
    _git_init(wrong_repo)
    _git_init(src_repo)

    monkeypatch.chdir(wrong_repo)
    monkeypatch.setenv("JUGGLE_TMUX_MOCK_PANE", "%mockpane")
    monkeypatch.setattr(ws, "canonical_repo_path",
                        lambda start=None: str(src_repo))

    mgr = jt.JuggleTmuxManager()
    agent = mgr.spawn_agent(db, "coder")

    assert agent["repo_path"] == str(src_repo), (
        f"agent bound to {agent['repo_path']}, expected canonical {src_repo}"
    )
    assert agent["repo_path"] != str(wrong_repo), (
        "agent mis-bound to orchestrator cwd repo (the 2026-06-16 incident)"
    )
