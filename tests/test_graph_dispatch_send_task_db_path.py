"""Regression pin: cmd_send_task must honor db_path injection (2026-06-11).

Symptom: autopilot graph dispatch creates agent in watchdog DB but cmd_send_task
opens default DB, reports "Agent not found", exits 1 → archived thread storm.
Root cause: cmd_send_task called bare _com.get_db() ignoring args.db_path.
"""
from __future__ import annotations

import os
import sys
import tempfile
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402


def _make_db(tmp_path: Path, name: str = "juggle.db") -> JuggleDB:
    db = JuggleDB(db_path=str(tmp_path / name))
    db.init_db()
    db.set_active(True)
    return db


# ---------------------------------------------------------------------------
# Cycle 1 — cmd_send_task resolves agent from injected db_path
# ---------------------------------------------------------------------------


def test_cmd_send_task_uses_injected_db_path(tmp_path):
    """cmd_send_task with db_path finds agent in the non-default temp DB.

    Incident 2026-06-11: cmd_send_task called _com.get_db() ignoring
    args.db_path, so an agent created in the watchdog DB was never found.
    """
    from juggle_cmd_agents_tasks import cmd_send_task

    # Isolated DB containing the agent — simulates the watchdog's DB.
    db = _make_db(tmp_path, "watchdog.db")
    thread_id = db.create_thread("graph-task-A", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(agent_id, status="idle", assigned_thread=thread_id)

    # Prompt file required by cmd_send_task.
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("do the thing")

    args = Namespace(
        agent_id=agent_id,
        prompt_file=str(prompt_path),
        no_template=False,
        worktree_path=None,
        worktree_branch=None,
        main_repo_path=None,
        allow_main=False,
        force_task=True,
        db_path=str(tmp_path / "watchdog.db"),
    )

    # Stub out tmux side-effects so the test stops after agent lookup.
    class _StopHere(Exception):
        pass

    with patch("juggle_cmd_agents_common.JuggleTmuxManager", side_effect=_StopHere):
        with patch("juggle_cmd_agents_graph.check_task_guard", return_value=None):
            # Should reach JuggleTmuxManager (raise _StopHere), NOT exit(1).
            # Reaching _StopHere proves the agent was found in the injected DB.
            try:
                cmd_send_task(args)
            except _StopHere:
                pass  # Agent found; stopped at tmux init — correct
            except SystemExit as e:
                pytest.fail(
                    f"cmd_send_task exited {e.code} — agent not found in injected DB. "
                    "db_path injection is broken."
                )


# ---------------------------------------------------------------------------
# Cycle 2 — _dispatch_via_pool passes db_path to cmd_send_task Namespace
# ---------------------------------------------------------------------------


def test_dispatch_via_pool_passes_db_path_to_send_task_to_agent(tmp_path, monkeypatch):
    """REGRESSION PIN (2026-06-11, rewritten 2026-06-20 for P3 seam):
    dispatch_node (called by _dispatch_via_pool) must pass db_path to
    send_task_to_agent so it opens the same database the tick created the
    agent in. Previously db_path was threaded through a cmd_send_task Namespace;
    now it is a kwarg to send_task_to_agent.
    """
    import juggle_graph_dispatch as gd
    import juggle_dispatch_core as _core

    db = _make_db(tmp_path)
    thread_id = db.create_thread("graph-task-B", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%1", harness="claude", repo_path="")

    captured: dict = {}

    def fake_acquire(db_, tid, **kw):
        return db_.get_agent(agent_id)

    def fake_send(db_, aid, tid, prompt, **kw):
        captured["db_path"] = kw.get("db_path")

    monkeypatch.setattr(_core, "acquire_agent", fake_acquire)
    monkeypatch.setattr(_core, "send_task_to_agent", fake_send)

    gd._dispatch_via_pool(db, thread_id, "do B", {"id": "B", "title": "B"})

    assert "db_path" in captured, "send_task_to_agent was never called with db_path"
    assert captured["db_path"] == str(db.db_path), (
        f"Expected db_path={db.db_path!r}, got {captured['db_path']!r}"
    )
