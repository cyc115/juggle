"""tests/test_add_node.py — TDD for P5 unified add-node verb (RED first).

Tests:
  - add_node kind=task, no deps → nodes row state='ready' + dual-written graph_tasks row
  - add_node kind=task, with deps → nodes row state='open' (dep unverified)
  - add_node kind=task, with verify_cmd → accepted
  - add_node kind=research, with verify_cmd → AddNodeError (kind guard)
  - add_node kind=conversation → nodes row + threads row; no owning-topic required
  - add_node cycle detection → AddNodeError
  - add_node unknown dep → AddNodeError
  - create-thread shim: still returns thread_id + creates node row
  - graph add-task shim via CLI: still creates graph_tasks row; also creates nodes row
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB
from dbops import db_graph as g


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


# ── add_node core ──────────────────────────────────────────────────────────────


def test_add_node_task_no_deps_state_ready(db):
    """add_node kind=task with no deps → nodes.state='ready'."""
    from juggle_add_node import add_node
    result = add_node(db, kind="task", title="Fix login", objective="do it")
    assert result["node_id"] is not None
    assert result["state"] == "ready"

    with db._connect() as conn:
        row = conn.execute(
            "SELECT state, kind FROM nodes WHERE id=?", (result["node_id"],)
        ).fetchone()
    assert row is not None
    assert row["state"] == "ready"
    assert row["kind"] == "task"


def test_add_node_task_no_deps_dual_writes_graph_tasks(db):
    """add_node kind=task → graph_tasks row exists (dual-write for legacy readers)."""
    from juggle_add_node import add_node
    result = add_node(db, kind="task", title="T", objective="do T")
    node_id = result["node_id"]

    task = g.get_task(db, node_id)
    assert task is not None, "graph_tasks dual-write missing"
    assert task["state"] in ("ready", "pending")  # recompute promotes to ready


def test_add_node_task_with_deps_state_open(db):
    """add_node kind=task with a dep that isn't verified → nodes.state='open'."""
    from juggle_add_node import add_node
    dep = add_node(db, kind="task", title="Dep", objective="build dep")
    # dep is ready but not verified → child stays open
    result = add_node(
        db, kind="task", title="Child", objective="build child",
        deps=[dep["node_id"]],
    )
    assert result["state"] == "open"


def test_add_node_verify_cmd_accepted_for_task(db):
    """--verify-cmd is accepted for kind=task."""
    from juggle_add_node import add_node
    result = add_node(db, kind="task", title="T", objective="do T",
                      verify_cmd="pytest -q")
    assert result["node_id"] is not None


def test_add_node_verify_cmd_rejected_for_research(db):
    """--verify-cmd rejected for kind=research (kind guard)."""
    from juggle_add_node import AddNodeError, add_node
    with pytest.raises(AddNodeError, match="verify.cmd"):
        add_node(db, kind="research", title="R", objective="research it",
                 verify_cmd="pytest -q")


def test_add_node_verify_cmd_rejected_for_conversation(db):
    """--verify-cmd rejected for kind=conversation."""
    from juggle_add_node import AddNodeError, add_node
    with pytest.raises(AddNodeError, match="verify.cmd"):
        add_node(db, kind="conversation", title="C", verify_cmd="pytest -q")


def test_add_node_conversation_no_project_required(db):
    """add_node kind=conversation with no project → does NOT raise 'unknown topic'."""
    from juggle_add_node import add_node
    result = add_node(db, kind="conversation", title="My convo")
    assert result["node_id"] is not None
    assert result["state"] == "open"


def test_add_node_conversation_dual_writes_thread(db):
    """add_node kind=conversation → threads row exists with same id."""
    from juggle_add_node import add_node
    result = add_node(db, kind="conversation", title="My convo")
    node_id = result["node_id"]

    thread = db.get_thread(node_id)
    assert thread is not None, "threads dual-write missing"
    assert thread["topic"] == "My convo"


def test_add_node_conversation_nodes_row_created(db):
    """add_node kind=conversation → nodes row exists."""
    from juggle_add_node import add_node
    result = add_node(db, kind="conversation", title="My convo")
    with db._connect() as conn:
        row = conn.execute(
            "SELECT id, kind, state FROM nodes WHERE id=?", (result["node_id"],)
        ).fetchone()
    assert row is not None
    assert row["kind"] == "conversation"
    assert row["state"] == "open"


def test_add_node_unknown_dep_rejected(db):
    """Dep that doesn't exist → AddNodeError."""
    from juggle_add_node import AddNodeError, add_node
    with pytest.raises(AddNodeError, match="unknown dep"):
        add_node(db, kind="task", title="T", objective="do T", deps=["ghost-id"])


def test_add_node_cycle_detection(db):
    """A dep chain that forms a cycle → AddNodeError."""
    from juggle_add_node import AddNodeError, add_node
    a = add_node(db, kind="task", title="A", objective="a")
    b = add_node(db, kind="task", title="B", objective="b", deps=[a["node_id"]])
    with pytest.raises(AddNodeError, match="cycle"):
        # would create: c depends on b; b depends on a; a required-by c → a→b→c→a
        add_node(db, kind="task", title="C", objective="c",
                 deps=[b["node_id"]], required_by=[a["node_id"]])


def test_add_node_default_project_inbox(db):
    """add_node with no --project → project_id is NULL (INBOX) in nodes."""
    from juggle_add_node import add_node
    result = add_node(db, kind="task", title="T", objective="do T")
    with db._connect() as conn:
        row = conn.execute(
            "SELECT project_id FROM nodes WHERE id=?", (result["node_id"],)
        ).fetchone()
    assert row is not None
    assert row["project_id"] is None


# ── create-thread shim ─────────────────────────────────────────────────────────


def test_create_thread_shim_returns_thread_id(db):
    """create-thread shim: db.create_thread still returns a valid thread id."""
    thread_id = db.create_thread("Test topic", session_id="")
    assert thread_id is not None
    thread = db.get_thread(thread_id)
    assert thread is not None
    assert thread["topic"] == "Test topic"


def test_create_thread_shim_also_creates_node(db):
    """P5 shim: cmd_create_thread writes a nodes row for the new thread."""
    from juggle_cmd_threads import _create_node_for_thread
    thread_id = db.create_thread("Topic X", session_id="")
    _create_node_for_thread(db, thread_id, "Topic X")
    with db._connect() as conn:
        row = conn.execute(
            "SELECT id, kind, state FROM nodes WHERE id=?", (thread_id,)
        ).fetchone()
    assert row is not None, "nodes row not created by shim helper"
    assert row["kind"] == "conversation"
    assert row["state"] == "open"


# ── graph add-task shim ────────────────────────────────────────────────────────


def test_graph_add_task_shim_still_creates_graph_tasks_row(db):
    """graph add-task shim: legacy graph_tasks row still written (backward compat)."""
    import juggle_cmd_graph as cg
    args = SimpleNamespace(
        project="INBOX", id="x", title="X", prompt="do x",
        deps=None, required_by=None, verify_cmd=None, json_out=False,
        topic=None, db_path=str(db.db_path),
    )
    cg.cmd_graph_add_task(args)
    assert g.get_task(db, "x") is not None


def test_graph_add_task_shim_also_creates_nodes_row(db):
    """P5 shim: graph add-task also writes a nodes row."""
    import juggle_cmd_graph as cg
    args = SimpleNamespace(
        project="INBOX", id="y", title="Y", prompt="do y",
        deps=None, required_by=None, verify_cmd=None, json_out=False,
        topic=None, db_path=str(db.db_path),
    )
    cg.cmd_graph_add_task(args)
    with db._connect() as conn:
        row = conn.execute("SELECT id, kind FROM nodes WHERE id='y'").fetchone()
    assert row is not None, "nodes row not written by graph add-task shim"
    assert row["kind"] == "task"


def test_graph_add_task_shim_validation_unknown_dep(db):
    """graph add-task shim still rejects unknown dep (guard preserved)."""
    import juggle_cmd_graph as cg
    args = SimpleNamespace(
        project="INBOX", id="x", title="X", prompt="do x",
        deps="ghost", required_by=None, verify_cmd=None, json_out=False,
        topic=None, db_path=str(db.db_path),
    )
    with pytest.raises(SystemExit) as ei:
        cg.cmd_graph_add_task(args)
    assert ei.value.code != 0
    assert g.get_task(db, "x") is None  # nothing written


def test_graph_add_task_shim_cycle_rejected(db):
    """graph add-task shim still rejects a cycle."""
    import juggle_cmd_graph as cg

    def _add(task_id, deps=None):
        a = SimpleNamespace(
            project="INBOX", id=task_id, title=task_id.upper(), prompt=f"do {task_id}",
            deps=deps, required_by=None, verify_cmd=None, json_out=False,
            topic=None, db_path=str(db.db_path),
        )
        cg.cmd_graph_add_task(a)

    _add("a")
    _add("b", deps="a")
    with pytest.raises(SystemExit) as ei:
        # d depends on b, required_by a → a→b→d→a cycle
        args = SimpleNamespace(
            project="INBOX", id="d", title="D", prompt="do d",
            deps="b", required_by="a", verify_cmd=None, json_out=False,
            topic=None, db_path=str(db.db_path),
        )
        cg.cmd_graph_add_task(args)
    assert ei.value.code != 0


# ── CLI add-node verb ──────────────────────────────────────────────────────────


def test_cli_add_node_json_output(db):
    """juggle add-node --json emits {node_id: ...}."""
    from juggle_cmd_add_node import cmd_add_node
    args = SimpleNamespace(
        kind="task", title="T", objective="do T",
        project=None, deps=None, required_by=None, verify_cmd=None,
        parent=None, json_out=True, db_path=str(db.db_path),
    )
    import io, contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cmd_add_node(args)
    payload = json.loads(out.getvalue())
    assert "node_id" in payload
    assert payload["node_id"] is not None


def test_cli_add_node_verify_cmd_non_task_exits(db, capsys):
    """juggle add-node --kind research --verify-cmd → nonzero exit."""
    from juggle_cmd_add_node import cmd_add_node
    args = SimpleNamespace(
        kind="research", title="R", objective="research",
        project=None, deps=None, required_by=None, verify_cmd="pytest -q",
        parent=None, json_out=False, db_path=str(db.db_path),
    )
    with pytest.raises(SystemExit) as ei:
        cmd_add_node(args)
    assert ei.value.code != 0
