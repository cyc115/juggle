"""Tests for P3: dispatch_node() extracted primitive.

2026-06-20: Verify tick routes through dispatch_node (not cmd_get_agent/cmd_send_task),
acquire_agent sets DB state, and cleanup on failure.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "dispatch.db"))
    d.init_db()
    d.set_active(True)
    return d


@pytest.fixture
def thread_id(db):
    return db.create_thread("dispatch-test", session_id="")


def _fake_mgr(pane_id="%fake"):
    mgr = MagicMock()
    mgr.wait_for_ready_to_paste.return_value = True
    mgr._run_tmux.return_value = MagicMock(returncode=0, stdout="")
    mgr.verify_pane.return_value = True
    mgr.send_task.return_value = "mock_hash"
    mgr.run_task_oneshot.return_value = ("mock_hash", None)
    return mgr


def _conv_state(db, tid):
    """Read the authoritative conversation-node state (P8: nodes, not threads)."""
    with db._connect() as conn:
        row = conn.execute(
            "SELECT state FROM nodes WHERE id=? AND kind='conversation'", (tid,)
        ).fetchone()
    return row["state"] if row else None


# ── Import gate ────────────────────────────────────────────────────────────────


def test_dispatch_node_importable():
    """juggle_dispatch_core must export dispatch_node, acquire_agent, send_task_to_agent."""
    from juggle_dispatch_core import dispatch_node, acquire_agent, send_task_to_agent  # noqa: F401


# ── Tick routes through dispatch_node ──────────────────────────────────────────


def test_dispatch_via_pool_calls_dispatch_node(db, thread_id, monkeypatch):
    """_dispatch_via_pool must delegate to dispatch_node, not cmd_get_agent/cmd_send_task."""
    import juggle_graph_dispatch as gd
    import juggle_dispatch_core as _core

    calls = []

    def fake_dispatch_node(db_, thread_id_, prompt_, task_, **kw):
        calls.append((thread_id_, task_["id"]))

    monkeypatch.setattr(_core, "dispatch_node", fake_dispatch_node)

    gd._dispatch_via_pool(db, thread_id, "test prompt", {"id": "task-1"})

    assert len(calls) == 1
    assert calls[0] == (thread_id, "task-1")


def test_dispatch_via_pool_does_not_call_cmd_agents():
    """_dispatch_via_pool source must not reference cmd_get_agent or cmd_send_task."""
    import juggle_graph_dispatch as gd

    src = inspect.getsource(gd._dispatch_via_pool)
    assert "cmd_get_agent" not in src, "tick must not call cmd_get_agent directly"
    assert "cmd_send_task" not in src, "tick must not call cmd_send_task directly"


# ── acquire_agent ──────────────────────────────────────────────────────────────


def test_acquire_agent_raises_capacity_error_when_pool_full(db, thread_id, monkeypatch):
    """acquire_agent raises CapacityError when agent pool is at MAX_BACKGROUND_AGENTS."""
    from juggle_dispatch_core import acquire_agent
    from juggle_graph_dispatch import CapacityError

    monkeypatch.setattr("juggle_db.MAX_BACKGROUND_AGENTS", 0)
    with pytest.raises(CapacityError):
        acquire_agent(db, thread_id, role="coder", _mgr=_fake_mgr())


def test_acquire_agent_spawns_new_sets_thread_background(db, thread_id, tmp_path, monkeypatch):
    """acquire_agent spawns a new agent and marks the conversation background.

    2026-06-27 P8 c3-write-cut/R3-2: nodes is the SOLE conversation writer for the
    'background' state — read it from nodes.state, NOT the now-uncut threads.status."""
    from juggle_dispatch_core import acquire_agent

    # Mock env so spawn_agent uses a fake pane without real tmux
    monkeypatch.setenv("JUGGLE_TMUX_MOCK_PANE", "%mock_spawn")
    monkeypatch.setenv("JUGGLE_TMUX_MOCK_SEND", "1")
    monkeypatch.setenv("JUGGLE_CLAUDE_JSON_PATH", str(tmp_path / ".claude.json"))
    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")

    agent = acquire_agent(db, thread_id, role="coder")

    assert agent is not None
    assert agent["status"] == "busy"
    assert agent["assigned_thread"] == thread_id
    assert _conv_state(db, thread_id) == "background"
    # write-cut: the legacy threads.status is no longer advanced to background —
    # only nodes.state is (get_thread now reads nodes, so check threads directly).
    with db._connect() as _c:
        _legacy = _c.execute(
            "SELECT status FROM threads WHERE id=?", (thread_id,)).fetchone()
    assert _legacy is None or _legacy["status"] != "background"


def test_acquire_agent_reuses_idle_agent_via_cas(db, thread_id, monkeypatch):
    """acquire_agent reuses an idle pool agent via CAS-assign."""
    from juggle_dispatch_core import acquire_agent

    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")
    existing_id = db.create_agent(role="coder", pane_id="%pool1", harness="claude", repo_path="")

    mgr = _fake_mgr(pane_id="%pool1")
    agent = acquire_agent(db, thread_id, role="coder", _mgr=mgr)

    assert agent["id"] == existing_id
    assert agent["status"] == "busy"
    assert agent["assigned_thread"] == thread_id
    assert _conv_state(db, thread_id) == "background"
    # write-cut: the legacy threads.status is no longer advanced to background —
    # only nodes.state is (get_thread now reads nodes, so check threads directly).
    with db._connect() as _c:
        _legacy = _c.execute(
            "SELECT status FROM threads WHERE id=?", (thread_id,)).fetchone()
    assert _legacy is None or _legacy["status"] != "background"


# ── dispatch_node composition ──────────────────────────────────────────────────


def test_dispatch_node_composes_acquire_and_send(db, thread_id, monkeypatch):
    """dispatch_node calls acquire_agent then send_task_to_agent."""
    import juggle_dispatch_core as _core

    acquire_calls = []
    send_calls = []

    def fake_acquire(db_, tid, **kw):
        acquire_calls.append(tid)
        return {"id": "fake-agent-1"}

    def fake_send(db_, agent_id, tid, prompt, **kw):
        send_calls.append((agent_id, tid, prompt))

    monkeypatch.setattr(_core, "acquire_agent", fake_acquire)
    monkeypatch.setattr(_core, "send_task_to_agent", fake_send)

    from juggle_dispatch_core import dispatch_node
    dispatch_node(db, thread_id, "my prompt", {"id": "t1"})

    assert acquire_calls == [thread_id]
    assert send_calls == [("fake-agent-1", thread_id, "my prompt")]


def test_dispatch_node_releases_agent_on_send_failure(db, thread_id, monkeypatch):
    """dispatch_node sets agent idle+unassigned when send_task_to_agent raises."""
    import juggle_dispatch_core as _core

    # Create a real agent so update_agent succeeds
    agent_id = db.create_agent(role="coder", pane_id="%p1", harness="claude", repo_path="")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id)

    def fake_acquire(db_, tid, **kw):
        return db_.get_agent(agent_id)

    def fake_send(db_, aid, tid, prompt, **kw):
        raise RuntimeError("tmux pane dead")

    monkeypatch.setattr(_core, "acquire_agent", fake_acquire)
    monkeypatch.setattr(_core, "send_task_to_agent", fake_send)

    from juggle_dispatch_core import dispatch_node
    with pytest.raises(RuntimeError, match="tmux pane dead"):
        dispatch_node(db, thread_id, "prompt", {"id": "t1"})

    agent = db.get_agent(agent_id)
    assert agent["status"] == "idle"
    assert agent["assigned_thread"] is None


def test_dispatch_node_propagates_capacity_error(db, thread_id, monkeypatch):
    """dispatch_node propagates CapacityError from acquire_agent."""
    import juggle_dispatch_core as _core
    from juggle_graph_dispatch import CapacityError

    def raise_cap(*a, **kw):
        raise CapacityError("full")
    monkeypatch.setattr(_core, "acquire_agent", raise_cap)

    from juggle_dispatch_core import dispatch_node
    with pytest.raises(CapacityError):
        dispatch_node(db, thread_id, "prompt", {"id": "t1"})


# ── dispatch binding as a typed node_edge (P8 M1/Q2) ───────────────────────────


def test_dispatch_binding_roundtrips_through_typed_edge(db):
    """2026-06-29 P8 M1/Q2: the task→dispatch-thread binding round-trips through a
    typed kind='dispatch' node_edge (not the nodes.dispatch_thread_id column), and
    dependency traversal (kind='dep') ignores the dispatch edge so a bound task
    is still ready-eligible."""
    from dbops import db_graph
    from juggle_add_node import add_node

    conv = db.create_thread("bound-conv", session_id="")
    r = add_node(db, kind="task", title="x", project_id="INBOX")
    tid = r["node_id"]

    db_graph.set_task_thread(db, tid, conv)

    # round-trips via both the by-thread reader and the projected thread_id field
    assert db_graph.get_task_by_thread(db, conv)["id"] == tid
    assert db_graph.get_task(db, tid)["thread_id"] == conv

    # the binding physically lives in node_edges as a typed dispatch edge
    with db._connect() as c:
        row = c.execute(
            "SELECT depends_on_id, kind FROM node_edges WHERE node_id=? AND kind='dispatch'",
            (tid,),
        ).fetchone()
    assert row is not None and row["depends_on_id"] == conv

    # the dispatch edge is NOT a dependency — dep traversal (kind='dep') ignores it
    assert db_graph.get_deps(db, tid) == []
    assert db_graph.unverified_deps(db, tid) == []


def test_dispatch_binding_clears_through_typed_edge(db):
    """2026-06-29 P8 M1/Q2: unbinding (set_task_thread(None)) deletes the typed
    dispatch edge — the by-thread reader and projected thread_id go empty."""
    from dbops import db_graph
    from juggle_add_node import add_node

    conv = db.create_thread("bound-conv-2", session_id="")
    r = add_node(db, kind="task", title="y", project_id="INBOX")
    tid = r["node_id"]
    db_graph.set_task_thread(db, tid, conv)
    db_graph.set_task_thread(db, tid, None)

    assert db_graph.get_task_by_thread(db, conv) is None
    assert db_graph.get_task(db, tid)["thread_id"] is None
    with db._connect() as c:
        n = c.execute(
            "SELECT COUNT(*) FROM node_edges WHERE node_id=? AND kind='dispatch'",
            (tid,),
        ).fetchone()[0]
    assert n == 0


# ── cmd_get_agent / cmd_send_task routing proof ────────────────────────────────


def test_cmd_get_agent_calls_acquire_agent(db, thread_id, monkeypatch, tmp_path):
    """cmd_get_agent must call juggle_dispatch_core.acquire_agent internally."""
    import juggle_dispatch_core as _core
    from juggle_cmd_agents_lifecycle import cmd_get_agent
    from argparse import Namespace

    acquire_calls = []

    def fake_acquire(db_, tid, **kw):
        acquire_calls.append(tid)
        # Minimal return to let cmd_get_agent print and exit
        agent_id = db_.create_agent(role="coder", pane_id="%p", harness="claude", repo_path="")
        db_.update_agent(agent_id, status="busy", assigned_thread=tid)
        return db_.get_agent(agent_id)

    monkeypatch.setattr(_core, "acquire_agent", fake_acquire)

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_get_agent(Namespace(
            thread_id=thread_id, role="coder", model=None,
            repo=None, harness=None, fresh=False,
            db_path=str(db.db_path),
        ))

    assert len(acquire_calls) == 1, "cmd_get_agent must call acquire_agent"
    assert acquire_calls[0] == thread_id


def test_cmd_send_task_calls_send_task_to_agent(db, thread_id, monkeypatch, tmp_path):
    """cmd_send_task must call juggle_dispatch_core.send_task_to_agent internally."""
    import juggle_dispatch_core as _core
    from juggle_cmd_agents_tasks import cmd_send_task
    from argparse import Namespace

    send_calls = []

    def fake_send(db_, agent_id, tid, prompt, **kw):
        send_calls.append((agent_id, tid))

    monkeypatch.setattr(_core, "send_task_to_agent", fake_send)

    # Set up an agent bound to the thread
    agent_id = db.create_agent(role="coder", pane_id="%p1", harness="claude", repo_path="")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id)

    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("do the work")

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_send_task(Namespace(
            agent_id=agent_id, prompt_file=str(prompt_file),
            no_template=False, worktree_path=None, worktree_branch=None,
            main_repo_path=None, allow_main=False, force_task=True,
            db_path=str(db.db_path),
        ))

    assert len(send_calls) == 1, "cmd_send_task must call send_task_to_agent"
    assert send_calls[0] == (agent_id, thread_id)
