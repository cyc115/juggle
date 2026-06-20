"""tests/test_p6_projects_as_tags.py — TDD for P6: projects as optional tags.

P6 behavior:
  - add-node with no --project → project_id=NULL in nodes (INBOX bucket)
  - add-task with missing/unknown topic → auto-attaches (NO "REFUSED" refusal)
  - ensure_inbox_project() creates the INBOX project idempotently
  - existing add-task callers that DO pass a topic → unchanged (back-compat)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


@pytest.fixture
def db_with_project(db):
    # create_project auto-assigns an ID ("P1", "P2" …); capture it.
    pid = db.create_project("Project One", "objective")
    db._test_project_id = pid  # attach for use in tests
    return db


# ── ensure_inbox_project ────────────────────────────────────────────────────


def test_ensure_inbox_project_creates_row(db):
    """ensure_inbox_project() creates an INBOX project row in the DB."""
    from juggle_add_node import ensure_inbox_project
    ensure_inbox_project(db)
    project = db.get_project("INBOX")
    assert project is not None
    assert project["id"] == "INBOX"


def test_ensure_inbox_project_idempotent(db):
    """ensure_inbox_project() called twice does not duplicate the row."""
    from juggle_add_node import ensure_inbox_project
    ensure_inbox_project(db)
    ensure_inbox_project(db)
    with db._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE id='INBOX'"
        ).fetchone()[0]
    assert count == 1


# ── add_node with no project → NULL project_id ─────────────────────────────


def test_add_node_no_project_null_in_db(db):
    """add_node with no project_id → nodes.project_id IS NULL."""
    from juggle_add_node import add_node
    result = add_node(db, kind="task", title="Inbox task", objective="do it")
    assert result["node_id"] is not None
    with db._connect() as conn:
        row = conn.execute(
            "SELECT project_id FROM nodes WHERE id=?", (result["node_id"],)
        ).fetchone()
    assert row is not None
    assert row["project_id"] is None


def test_add_node_no_project_succeeds_no_error(db):
    """add_node with no --project succeeds without error (no mandatory project)."""
    from juggle_add_node import add_node
    result = add_node(db, kind="research", title="Research topic", objective="investigate")
    assert result["node_id"] is not None
    assert result["state"] == "open"


def test_add_node_with_project_back_compat(db_with_project):
    """add_node with explicit project_id → tagged to that project (back-compat)."""
    from juggle_add_node import add_node
    pid = db_with_project._test_project_id
    result = add_node(
        db_with_project, kind="task", title="Tagged task",
        objective="do it", project_id=pid,
    )
    with db_with_project._connect() as conn:
        row = conn.execute(
            "SELECT project_id FROM nodes WHERE id=?", (result["node_id"],)
        ).fetchone()
    assert row["project_id"] == pid


# ── add-task missing/unknown topic → no refusal ─────────────────────────────


def _make_add_task_args(db, **kwargs):
    """Build a SimpleNamespace args object for cmd_graph_add_task."""
    return SimpleNamespace(
        project=kwargs.get("project", "INBOX"),
        id=kwargs.get("id", "task-1"),
        title=kwargs.get("title", "My Task"),
        prompt=kwargs.get("prompt", "Do the task"),
        topic=kwargs.get("topic", None),
        deps=kwargs.get("deps", ""),
        required_by=kwargs.get("required_by", ""),
        verify_cmd=kwargs.get("verify_cmd", None),
        json_out=kwargs.get("json_out", True),
        db_path=db.db_path,
    )


def test_add_task_missing_topic_on_topic_project_no_refusal(db_with_project):
    """add-task with no --topic on a project with real topics → auto-attaches (no refusal).

    Pre-P6: this path REFUSED with 'this project has topics; --topic is required'.
    Post-P6: routes through add_node → succeeds.
    """
    import juggle_cmd_graph as cg
    from dbops import db_topics

    pid = db_with_project._test_project_id
    # Create a real topic so has_real_topic=True
    db_topics.create_topic(db_with_project, topic_id="t1", project_id=pid, title="T1")

    import json
    args = _make_add_task_args(
        db_with_project, project=pid, topic=None, json_out=True
    )

    output = []
    def fake_print(s="", **kw):
        output.append(s)

    import builtins
    orig_print = builtins.print
    builtins.print = fake_print
    try:
        cg.cmd_graph_add_task(args)
    except SystemExit as e:
        builtins.print = orig_print
        pytest.fail(f"cmd_graph_add_task REFUSED (exit {e.code}): {output}")
    finally:
        builtins.print = orig_print

    combined = " ".join(output)
    assert "REFUSED" not in combined


def test_add_task_unknown_topic_no_refusal(db_with_project):
    """add-task with --topic <nonexistent> → auto-attaches (no refusal).

    Pre-P6: this path REFUSED with 'unknown topic <x>'.
    Post-P6: routes through add_node → succeeds.
    """
    import juggle_cmd_graph as cg

    pid = db_with_project._test_project_id
    args = _make_add_task_args(
        db_with_project,
        project=pid, topic="nonexistent-topic-xyz", json_out=True,
    )

    output = []
    def fake_print(s="", **kw):
        output.append(s)

    import builtins
    orig_print = builtins.print
    builtins.print = fake_print
    try:
        cg.cmd_graph_add_task(args)
    except SystemExit as e:
        builtins.print = orig_print
        pytest.fail(f"cmd_graph_add_task REFUSED (exit {e.code}): {output}")
    finally:
        builtins.print = orig_print

    combined = " ".join(output)
    assert "REFUSED" not in combined
    assert "unknown topic" not in combined


def test_add_task_with_known_topic_back_compat(db_with_project):
    """add-task with a known --topic still works (back-compat)."""
    import juggle_cmd_graph as cg
    from dbops import db_topics

    pid = db_with_project._test_project_id
    db_topics.create_topic(db_with_project, topic_id="t1", project_id=pid, title="T1")

    args = _make_add_task_args(
        db_with_project,
        project=pid, topic="t1", json_out=True,
    )

    output = []
    def fake_print(s="", **kw):
        output.append(s)

    import builtins
    orig_print = builtins.print
    builtins.print = fake_print
    try:
        cg.cmd_graph_add_task(args)
    except SystemExit as e:
        builtins.print = orig_print
        pytest.fail(f"cmd_graph_add_task failed (exit {e.code}): {output}")
    finally:
        builtins.print = orig_print

    combined = " ".join(output)
    assert "REFUSED" not in combined
