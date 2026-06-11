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
    thread_id = db.create_thread("graph-node-A", session_id="")
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
        force_node=True,
        db_path=str(tmp_path / "watchdog.db"),
    )

    # Stub out tmux side-effects so the test stops after agent lookup.
    class _StopHere(Exception):
        pass

    with patch("juggle_cmd_agents_common.JuggleTmuxManager", side_effect=_StopHere):
        with patch("juggle_cmd_agents_graph.check_node_guard", return_value=None):
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


def test_dispatch_via_pool_passes_db_path_to_send_task(tmp_path, monkeypatch):
    """_dispatch_via_pool includes db_path in the Namespace for cmd_send_task.

    Incident 2026-06-11: Namespace passed to cmd_send_task omitted db_path,
    causing it to open the default DB and miss the watchdog-created agent.
    """
    import juggle_graph_dispatch as gd
    from dbops import db_graph as g

    db = _make_db(tmp_path)
    thread_id = db.create_thread("graph-node-B", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(agent_id, status="idle", assigned_thread=thread_id)

    g.create_node(db, node_id="B", project_id="INBOX", title="B", prompt="do B")
    node = g.get_node(db, "B")

    captured: list[Namespace] = []

    def fake_get_agent(ns):
        pass  # no-op: agent already bound

    def fake_send_task(ns):
        captured.append(ns)

    monkeypatch.setattr("juggle_cmd_agents.cmd_get_agent", fake_get_agent)
    monkeypatch.setattr("juggle_cmd_agents.cmd_send_task", fake_send_task)
    # Stub get_agent_by_thread to return a fake agent.
    monkeypatch.setattr(db, "get_agent_by_thread", lambda _tid: {"id": agent_id})

    gd._dispatch_via_pool(db, thread_id, "do B", node)

    assert captured, "_dispatch_via_pool never called cmd_send_task"
    ns = captured[0]
    assert hasattr(ns, "db_path"), "Namespace missing db_path"
    assert ns.db_path == str(db.db_path), (
        f"Expected db_path={db.db_path!r}, got {ns.db_path!r}"
    )
