"""Tests for `juggle graph learnings` (gl-learnings-cmd) — the token-light
read primitive over the nodes.learnings column (Migration 70).

Covers the reader helper (db_graph.read_learnings) across the three scopes
(node-id / --topic / --project), completion-order output, the
`N nodes, M with learnings` footer, empty-scope exit 0, node-not-found exit
nonzero, and the --json shape from Final revisions (8).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
import juggle_cmd_graph as cg  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


def _task(db, task_id, *, project="P1", topic=None, learnings=None, verified_at=None):
    """Insert a kind='task' node with optional learnings + verified_at."""
    g.create_task(db, task_id=task_id, project_id=project, title=task_id, prompt="x")
    if topic is not None:
        g.set_task_topic(db, task_id, topic)
    with db._connect() as c:
        c.execute(
            "UPDATE nodes SET learnings=?, verified_at=? WHERE id=? AND kind='task'",
            (learnings, verified_at, task_id),
        )
        c.commit()


def _args(db, node_id=None, topic=None, project=None, json_out=False):
    return SimpleNamespace(
        node_id=node_id, topic=topic, project=project,
        json_out=json_out, db_path=str(db.db_path),
    )


# ── reader helper ──────────────────────────────────────────────────────────────


def test_read_learnings_node_scope(db):
    _task(db, "n1", learnings="use flag X not Y", verified_at="2026-07-03T01:00:00")
    r = g.read_learnings(db, node_id="n1")
    assert r == {
        "items": [{"node_id": "n1", "text": "use flag X not Y",
                   "completed_at": "2026-07-03T01:00:00"}],
        "nodes": 1, "with_learnings": 1,
    }


def test_read_learnings_missing_node(db):
    r = g.read_learnings(db, node_id="nope")
    assert r["nodes"] == 0 and r["items"] == []


def test_read_learnings_topic_completion_order(db):
    # a completed first, c completed later, b has no learnings
    _task(db, "a", topic="T", learnings="alpha", verified_at="2026-07-03T01:00:00")
    _task(db, "b", topic="T")
    _task(db, "c", topic="T", learnings="gamma", verified_at="2026-07-03T02:00:00")
    r = g.read_learnings(db, topic_id="T")
    assert [it["node_id"] for it in r["items"]] == ["a", "c"]
    assert r["nodes"] == 3 and r["with_learnings"] == 2


def test_read_learnings_blank_is_empty(db):
    _task(db, "a", topic="T", learnings="   ", verified_at="2026-07-03T01:00:00")
    r = g.read_learnings(db, topic_id="T")
    assert r["nodes"] == 1 and r["with_learnings"] == 0 and r["items"] == []


def test_read_learnings_project_scope(db):
    _task(db, "a", project="P1", learnings="keep")
    _task(db, "b", project="P2", learnings="other")
    r = g.read_learnings(db, project_id="P1")
    assert [it["node_id"] for it in r["items"]] == ["a"]
    assert r["nodes"] == 1


# ── command output ─────────────────────────────────────────────────────────────


def test_cmd_node_plain(db, capsys):
    _task(db, "n1", learnings="use flag X", verified_at="2026-07-03T01:00:00")
    cg.cmd_graph_learnings(_args(db, node_id="n1"))
    out = capsys.readouterr().out
    assert "n1: use flag X" in out
    assert "1 nodes, 1 with learnings" in out


def test_cmd_missing_node_exits_nonzero(db, capsys):
    with pytest.raises(SystemExit) as e:
        cg.cmd_graph_learnings(_args(db, node_id="ghost"))
    assert e.value.code != 0


def test_cmd_empty_scope_footer_only_exit0(db, capsys):
    _task(db, "a", topic="T")  # exists, no learnings
    cg.cmd_graph_learnings(_args(db, topic="T"))
    out = capsys.readouterr().out.strip()
    assert out == "1 nodes, 0 with learnings"


def test_cmd_json_shape(db, capsys):
    _task(db, "a", topic="T", learnings="alpha", verified_at="2026-07-03T01:00:00")
    _task(db, "b", topic="T")
    cg.cmd_graph_learnings(_args(db, topic="T", json_out=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "items": [{"node_id": "a", "text": "alpha",
                   "completed_at": "2026-07-03T01:00:00"}],
        "nodes": 2, "with_learnings": 1,
    }


# ── parser wiring ──────────────────────────────────────────────────────────────


def test_parser_registers_learnings():
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    cg.register_graph_parsers(sub)
    ns = parser.parse_args(["graph", "learnings", "n1"])
    assert ns.node_id == "n1"
    assert ns.func is cg.cmd_graph_learnings
    ns2 = parser.parse_args(["graph", "learnings", "--topic", "T", "--json"])
    assert ns2.topic == "T" and ns2.json_out is True
