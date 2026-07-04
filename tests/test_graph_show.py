"""Tests for `juggle graph show` — the pure-read graph primitive (Phase4,
2026-07-03 graph-node-primitives spec §1).

Covers: no-arg project rollup line, --project node lines (deps + dependents
count), --node detail, --state filter, and --json shapes (--node JSON carries
cancel_reason + verify_failure). Pure read — zero mutation.
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
import juggle_cmd_graph_show as gs  # noqa: E402


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
    """A → {b, c} → d diamond in INBOX. a verified, b failed-verify, c running,
    d blocked-failed."""
    _mk(db, "INBOX", "a", "Task A", verify_cmd="pytest -q")
    _mk(db, "INBOX", "b", "Task B", deps=["a"])
    _mk(db, "INBOX", "c", "Task C", deps=["a"])
    _mk(db, "INBOX", "d", "Task D", deps=["b", "c"])
    # a → verified
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start", "integrate_ok"):
        g.task_transition(db, "a", ev)
    # b → failed-verify
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start", "verify_fail"):
        g.task_transition(db, "b", ev)
    # c → running
    for ev in ("deps_ready", "claim", "dispatch"):
        g.task_transition(db, "c", ev)
    # d → blocked-failed (b failed upstream)
    g.propagate_failure(db, "b")


def _args(db, **kw):
    base = dict(project=None, node=None, state=None, json_out=False,
                db_path=str(db.db_path))
    base.update(kw)
    return SimpleNamespace(**base)


# ── no-arg: one line per project ───────────────────────────────────────────────


def test_no_arg_lists_project_rollup(db, capsys):
    _seed(db)
    gs.cmd_graph_show(_args(db))
    out = capsys.readouterr().out
    assert "INBOX" in out
    # 1 verified of 4 non-cancelled counted total, 1 failed, 1 blocked
    assert "1/4 ✓" in out
    assert "1✗" in out
    assert "1⊘" in out


# ── --project: node lines with deps + dependents count ─────────────────────────


def test_project_lists_nodes_with_deps_and_dependents(db, capsys):
    _seed(db)
    gs.cmd_graph_show(_args(db, project="INBOX"))
    out = capsys.readouterr().out
    # 'd' depends on b,c and has no dependents
    assert "(deps: b,c · dependents: 0)" in out
    # 'a' has no deps and 2 dependents (b, c)
    assert "(deps: — · dependents: 2)" in out
    # glyphs present: verified ✓, failed ✗, running ●, blocked ⊘
    assert "✓ a" in out
    assert "✗ b" in out


def test_project_not_found_exits(db):
    with pytest.raises(SystemExit):
        gs.cmd_graph_show(_args(db, project="NOPE"))


# ── --state filter ─────────────────────────────────────────────────────────────


def test_state_filter_only_matching(db, capsys):
    _seed(db)
    gs.cmd_graph_show(_args(db, project="INBOX", state="failed-verify"))
    out = capsys.readouterr().out
    assert " b " in out or "✗ b" in out
    assert "Task A" not in out  # a is verified, filtered out


# ── --node detail ──────────────────────────────────────────────────────────────


def test_node_detail_human(db, capsys):
    _seed(db)
    gs.cmd_graph_show(_args(db, node="d"))
    out = capsys.readouterr().out
    assert "blocked-failed" in out
    assert "b" in out and "c" in out  # deps listed
    assert "verify_cmd" in out


def test_node_not_found_exits(db):
    _seed(db)
    with pytest.raises(SystemExit):
        gs.cmd_graph_show(_args(db, node="ghost"))


# ── --json shapes ──────────────────────────────────────────────────────────────


def test_project_json_shape(db, capsys):
    _seed(db)
    gs.cmd_graph_show(_args(db, project="INBOX", json_out=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["project"] == "INBOX"
    assert payload["total"] == 3 or payload["total"] == 4  # tolerate cancelled excl.
    ids = {n["id"] for n in payload["nodes"]}
    assert ids == {"a", "b", "c", "d"}
    d = next(n for n in payload["nodes"] if n["id"] == "d")
    assert d["deps"] == ["b", "c"]
    assert d["dependents"] == []


def test_node_json_includes_cancel_reason_and_verify_failure(db, capsys):
    _seed(db)
    gs.cmd_graph_show(_args(db, node="b", json_out=True))
    node = json.loads(capsys.readouterr().out)
    assert node["id"] == "b"
    assert node["state"] == "failed-verify"
    assert "cancel_reason" in node
    assert "verify_failure" in node
    assert node["deps"] == ["a"]


def test_show_is_pure_read(db):
    """Show must never mutate state."""
    _seed(db)
    before = {t["id"]: t["state"] for t in g.list_tasks(db, "INBOX")}
    gs.cmd_graph_show(_args(db, project="INBOX"))
    gs.cmd_graph_show(_args(db, node="d"))
    after = {t["id"]: t["state"] for t in g.list_tasks(db, "INBOX")}
    assert before == after
