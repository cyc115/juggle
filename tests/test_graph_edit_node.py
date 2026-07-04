"""Tests for `juggle graph edit-node` — UPDATE fields + edges (Phase7,
2026-07-03 graph-node-primitives spec §2).

Covers: field edits (title/prompt/priority/verify-cmd), --prompt via stdin,
surgical edge edits (--add-dep / --rm-dep), the MUTABLE_STATES transition guard
(exit 2 on verified/cancelled/in-flight), cycle refusal (exit 2, reuses
find_cycle), unknown add-dep target (exit 1), missing node (exit 1), --json
shape, and the human change-summary line.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
import juggle_cmd_graph_ops as ops  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


def _mk(db, pid, task_id, title, *, deps=None, verify_cmd=None):
    g.create_task(db, task_id=task_id, project_id=pid, title=title,
                  prompt=f"do {task_id}", verify_cmd=verify_cmd)
    if deps:
        g.replace_edges(db, task_id, deps)


def _seed(db):
    """a → b → c chain plus a standalone d, all 'open' (mutable)."""
    _mk(db, "INBOX", "a", "Task A")
    _mk(db, "INBOX", "b", "Task B", deps=["a"])
    _mk(db, "INBOX", "c", "Task C", deps=["b"])
    _mk(db, "INBOX", "d", "Task D")


def _args(db, node_id, **kw):
    base = dict(id=node_id, title=None, prompt=None, priority=None,
                verify_cmd=None, add_dep=None, rm_dep=None, json_out=False,
                db_path=str(db.db_path))
    base.update(kw)
    return SimpleNamespace(**base)


# ── field edits ────────────────────────────────────────────────────────────────


def test_edit_title(db, capsys):
    _seed(db)
    ops.cmd_graph_edit_node(_args(db, "a", title="Renamed A"))
    assert g.get_task(db, "a")["title"] == "Renamed A"


def test_edit_prompt_inline(db):
    _seed(db)
    ops.cmd_graph_edit_node(_args(db, "a", prompt="fresh prompt"))
    assert g.get_task(db, "a")["prompt"] == "fresh prompt"


def test_edit_prompt_from_stdin(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr("sys.stdin", io.StringIO("piped prompt body"))
    ops.cmd_graph_edit_node(_args(db, "a", prompt="-"))
    assert g.get_task(db, "a")["prompt"] == "piped prompt body"


def test_edit_priority(db):
    _seed(db)
    ops.cmd_graph_edit_node(_args(db, "a", priority=50))
    assert g.get_task(db, "a")["priority"] == 50


def test_edit_verify_cmd(db):
    _seed(db)
    ops.cmd_graph_edit_node(_args(db, "a", verify_cmd="pytest -q"))
    assert g.get_task(db, "a")["verify_cmd"] == "pytest -q"


def test_edit_empty_verify_cmd_refused(db):
    _seed(db)
    with pytest.raises(SystemExit) as exc:
        ops.cmd_graph_edit_node(_args(db, "a", verify_cmd="   "))
    assert exc.value.code == 1


# ── surgical edge edits ────────────────────────────────────────────────────────


def test_add_dep(db):
    _seed(db)
    ops.cmd_graph_edit_node(_args(db, "d", add_dep="a,b"))
    assert g.get_deps(db, "d") == ["a", "b"]


def test_rm_dep(db):
    _seed(db)
    ops.cmd_graph_edit_node(_args(db, "c", rm_dep="b"))
    assert g.get_deps(db, "c") == []


def test_add_and_rm_dep_together(db):
    _seed(db)
    ops.cmd_graph_edit_node(_args(db, "c", add_dep="a", rm_dep="b"))
    assert g.get_deps(db, "c") == ["a"]


def test_add_dep_unknown_target_refused(db):
    _seed(db)
    with pytest.raises(SystemExit) as exc:
        ops.cmd_graph_edit_node(_args(db, "d", add_dep="ghost"))
    assert exc.value.code == 1
    assert g.get_deps(db, "d") == []  # no partial write


def test_add_dep_self_refused(db):
    _seed(db)
    with pytest.raises(SystemExit) as exc:
        ops.cmd_graph_edit_node(_args(db, "d", add_dep="d"))
    assert exc.value.code == 1


def test_add_dep_cycle_refused(db):
    """a→b→c already; adding a dep a→c closes a cycle → refuse exit 2."""
    _seed(db)
    with pytest.raises(SystemExit) as exc:
        ops.cmd_graph_edit_node(_args(db, "a", add_dep="c"))
    assert exc.value.code == 2
    assert g.get_deps(db, "a") == []  # unchanged


# ── transition guard (MUTABLE_STATES) ──────────────────────────────────────────


def test_edit_verified_refused_exit2(db):
    _seed(db)
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start", "integrate_ok"):
        g.task_transition(db, "a", ev)
    with pytest.raises(SystemExit) as exc:
        ops.cmd_graph_edit_node(_args(db, "a", title="nope"))
    assert exc.value.code == 2
    assert g.get_task(db, "a")["title"] == "Task A"  # unchanged


def test_edit_running_refused_exit2(db):
    _seed(db)
    for ev in ("deps_ready", "claim", "dispatch"):
        g.task_transition(db, "a", ev)
    with pytest.raises(SystemExit) as exc:
        ops.cmd_graph_edit_node(_args(db, "a", title="nope"))
    assert exc.value.code == 2


def test_edit_cancelled_refused_exit2(db):
    _seed(db)
    g.task_transition(db, "d", "cancel")
    with pytest.raises(SystemExit) as exc:
        ops.cmd_graph_edit_node(_args(db, "d", title="nope"))
    assert exc.value.code == 2


def test_edit_failed_verify_allowed(db):
    """failed-verify is in MUTABLE_STATES — edit is allowed."""
    _seed(db)
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start", "verify_fail"):
        g.task_transition(db, "a", ev)
    ops.cmd_graph_edit_node(_args(db, "a", title="Retry A"))
    assert g.get_task(db, "a")["title"] == "Retry A"


# ── not found ───────────────────────────────────────────────────────────────────


def test_edit_missing_node_exit1(db):
    _seed(db)
    with pytest.raises(SystemExit) as exc:
        ops.cmd_graph_edit_node(_args(db, "ghost", title="x"))
    assert exc.value.code == 1


# ── output ──────────────────────────────────────────────────────────────────────


def test_human_summary_lists_changes(db, capsys):
    _seed(db)
    ops.cmd_graph_edit_node(_args(db, "d", title="D2", add_dep="a"))
    out = capsys.readouterr().out
    assert "d" in out
    assert "title" in out
    assert "+dep a" in out


def test_json_emits_node_object(db, capsys):
    _seed(db)
    ops.cmd_graph_edit_node(_args(db, "d", title="D2", add_dep="a", json_out=True))
    node = json.loads(capsys.readouterr().out)
    assert node["id"] == "d"
    assert node["title"] == "D2"
    assert node["deps"] == ["a"]
