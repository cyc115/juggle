"""Tests for `juggle graph add-node` (single-node mid-execution graph insert).

Covers the shared juggle_graph_upsert.add_node/validate_add_node path and the
cmd_graph_add_node CLI handler: live insert, unknown-dep/cycle/empty-prompt/
verify_cmd rejection, the protected-state guard on --required-by and re-added
ids, --deps state-driven initial readiness, downstream demotion, and atomic
all-or-nothing refusal.

These pin the guard + atomicity invariants of the feature (CLAUDE.md: feature
pins still required for guard/atomicity).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
import juggle_cmd_graph as cg  # noqa: E402
import juggle_graph_add as up  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


def _diamond(db):
    """a → (b, c) → d, root promoted to ready."""
    g.create_node(db, node_id="a", project_id="INBOX", title="A", prompt="build a")
    g.create_node(db, node_id="b", project_id="INBOX", title="B", prompt="build b")
    g.create_node(db, node_id="c", project_id="INBOX", title="C", prompt="build c")
    g.create_node(db, node_id="d", project_id="INBOX", title="D", prompt="build d")
    g.replace_edges(db, "b", ["a"])
    g.replace_edges(db, "c", ["a"])
    g.replace_edges(db, "d", ["b", "c"])
    g.recompute_ready(db, "INBOX")


def _walk(db, node_id, *events):
    for ev in events:
        g.node_transition(db, node_id, ev)


# ── happy path: add into a live graph ──────────────────────────────────────────


def test_add_node_inserts_into_live_graph(db):
    _diamond(db)
    res = up.add_node(
        db, "INBOX", node_id="x", title="X", prompt="do x",
        deps=["a"], required_by=[], verify_cmd=None,
    )
    assert res["node_id"] == "x"
    assert g.get_node(db, "x") is not None
    assert g.get_deps(db, "x") == ["a"]
    # a is ready (pending root promoted), not verified → x pending
    assert res["state"] == "pending"


def test_add_node_no_deps_is_ready_immediately(db):
    _diamond(db)
    res = up.add_node(
        db, "INBOX", node_id="x", title="X", prompt="do x",
        deps=[], required_by=[], verify_cmd=None,
    )
    assert res["state"] == "ready"


# ── --deps state drives initial readiness ──────────────────────────────────────


def test_deps_on_verified_node_makes_new_node_ready(db):
    _diamond(db)
    _walk(db, "a", "claim", "dispatch", "integrate_start", "integrate_ok")
    res = up.add_node(
        db, "INBOX", node_id="x", title="X", prompt="do x",
        deps=["a"], required_by=[], verify_cmd=None,
    )
    assert g.get_node(db, "a")["state"] == "verified"
    assert res["state"] == "ready"


def test_deps_on_running_node_keeps_new_node_pending(db):
    _diamond(db)
    _walk(db, "a", "claim", "dispatch")  # a now running (any state OK upstream)
    res = up.add_node(
        db, "INBOX", node_id="x", title="X", prompt="do x",
        deps=["a"], required_by=[], verify_cmd=None,
    )
    assert g.get_node(db, "a")["state"] == "running"
    assert res["state"] == "pending"


# ── validation rejections (nothing written) ────────────────────────────────────


def test_unknown_dep_rejected(db):
    _diamond(db)
    with pytest.raises(up.AddNodeError, match="unknown dep"):
        up.validate_add_node(
            db, "INBOX", node_id="x", title="X", prompt="do x",
            deps=["ghost"], required_by=[], verify_cmd=None,
        )


def test_empty_prompt_rejected(db):
    _diamond(db)
    with pytest.raises(up.AddNodeError, match="empty prompt"):
        up.validate_add_node(
            db, "INBOX", node_id="x", title="X", prompt="   ",
            deps=["a"], required_by=[], verify_cmd=None,
        )


def test_verify_cmd_lint_rejected(db):
    _diamond(db)
    with pytest.raises(up.AddNodeError, match="verify_cmd"):
        up.validate_add_node(
            db, "INBOX", node_id="x", title="X", prompt="do x",
            deps=["a"], required_by=[], verify_cmd="rm -rf /",
        )


def test_cycle_via_required_by_rejected(db):
    """--required-by a that closes a→b→...→a would form a loop: a depends on x,
    x depends on a's downstream → cycle. Here: x deps on d, required-by a."""
    _diamond(db)
    with pytest.raises(up.AddNodeError, match="cycle"):
        up.validate_add_node(
            db, "INBOX", node_id="x", title="X", prompt="do x",
            deps=["d"], required_by=["a"], verify_cmd=None,
        )


# ── GUARD: protected-state nodes refuse edges ──────────────────────────────────


def test_required_by_running_node_refused(db):
    """GUARD PIN: a --required-by target in a protected state (running) is
    refused — you cannot add a dependency to a node already executing."""
    _diamond(db)
    _walk(db, "a", "claim", "dispatch")  # a running (protected)
    with pytest.raises(up.AddNodeError, match="protected"):
        up.validate_add_node(
            db, "INBOX", node_id="x", title="X", prompt="do x",
            deps=[], required_by=["a"], verify_cmd=None,
        )


def test_required_by_verified_node_refused(db):
    """GUARD PIN: a --required-by target that is already 'verified' is refused
    (it is done — adding an unfinished dep can't un-verify it)."""
    _diamond(db)
    _walk(db, "a", "claim", "dispatch", "integrate_start", "integrate_ok")
    with pytest.raises(up.AddNodeError, match="protected"):
        up.validate_add_node(
            db, "INBOX", node_id="x", title="X", prompt="do x",
            deps=[], required_by=["a"], verify_cmd=None,
        )


def test_required_by_pending_node_accepted_and_demotes(db):
    """GUARD PIN: a --required-by target in a mutable state (here d is pending)
    is accepted; d gains a dep on the new unfinished node. d was pending and
    stays pending (the new node is not verified)."""
    _diamond(db)
    assert g.get_node(db, "d")["state"] == "pending"
    res = up.add_node(
        db, "INBOX", node_id="x", title="X", prompt="do x",
        deps=[], required_by=["d"], verify_cmd=None,
    )
    assert "x" in g.get_deps(db, "d")
    assert g.get_node(db, "d")["state"] == "pending"
    # x has no deps → ready immediately
    assert res["state"] == "ready"


def test_required_by_ready_node_is_demoted_to_pending(db):
    """GUARD PIN: a --required-by target that was 'ready' must be demoted to
    'pending' once it gains an unverified dep (the new node). Use the readiness
    recompute seam — node_transition stays the sole state writer."""
    g.create_node(db, node_id="root", project_id="INBOX", title="R", prompt="r")
    g.recompute_ready(db, "INBOX")
    assert g.get_node(db, "root")["state"] == "ready"
    res = up.add_node(
        db, "INBOX", node_id="pre", title="Pre", prompt="run before root",
        deps=[], required_by=["root"], verify_cmd=None,
    )
    assert g.get_node(db, "root")["state"] == "pending"  # demoted
    assert {"id": "root", "from": "ready", "to": "pending"} in res["downstream_changed"]


def test_re_add_protected_id_refused(db):
    """GUARD PIN: re-adding an --id that already exists in a protected state
    (running) is refused — cannot overwrite an executing node."""
    _diamond(db)
    _walk(db, "a", "claim", "dispatch")
    with pytest.raises(up.AddNodeError, match="protected"):
        up.validate_add_node(
            db, "INBOX", node_id="a", title="A2", prompt="redo a",
            deps=[], required_by=[], verify_cmd=None,
        )


# ── ATOMICITY: a rejected add leaves the graph byte-identical ───────────────────


def test_rejected_add_leaves_graph_unchanged(db):
    """ATOMICITY PIN: a refused add (cycle here) must write NOTHING — the live
    graph's nodes, states, and edges are identical to before the call."""
    _diamond(db)

    def _snapshot():
        nodes = {n["id"]: (n["state"], n["title"], n["prompt"]) for n in g.list_nodes(db, "INBOX")}
        edges = {nid: g.get_deps(db, nid) for nid in nodes}
        return nodes, edges

    before = _snapshot()
    with pytest.raises(up.AddNodeError):
        up.add_node(
            db, "INBOX", node_id="x", title="X", prompt="do x",
            deps=["d"], required_by=["a"], verify_cmd=None,  # forms a cycle
        )
    assert _snapshot() == before


def test_required_by_demotion_is_atomic_with_edge_write(db, monkeypatch):
    """ATOMICITY PIN (DA self-review, 2026-06-10): the demotion of a 'ready'
    --required-by target must commit in the SAME transaction as its new edge —
    the dispatcher claims any state='ready' node WITHOUT re-checking deps, so a
    crash between commit and a post-commit demote would dispatch a node whose
    new dep is unverified. Simulate a crash in the post-commit recompute and
    assert the target is ALREADY demoted (pending) and the new node persisted."""
    g.create_node(db, node_id="root", project_id="INBOX", title="R", prompt="r")
    g.recompute_ready(db, "INBOX")
    assert g.get_node(db, "root")["state"] == "ready"

    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash post-commit"))
    monkeypatch.setattr(g, "recompute_ready", boom)
    with pytest.raises(RuntimeError):
        up.add_node(
            db, "INBOX", node_id="pre", title="Pre", prompt="before root",
            deps=[], required_by=["root"], verify_cmd=None,
        )
    # edge write + demotion already committed before the post-commit recompute
    assert g.get_node(db, "pre") is not None
    assert "pre" in g.get_deps(db, "root")
    assert g.get_node(db, "root")["state"] == "pending"  # NOT stale-ready


# ── CLI handler ────────────────────────────────────────────────────────────────


def _args(db, **kw):
    base = dict(
        project="INBOX", id="x", title="X", prompt="do x",
        deps=None, required_by=None, verify_cmd=None, json_out=False,
        db_path=str(db.db_path),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_cli_add_node_success(db, capsys):
    _diamond(db)
    cg.cmd_graph_add_node(_args(db, deps="a"))
    assert g.get_node(db, "x") is not None
    out = capsys.readouterr().out
    assert "x" in out and "pending" in out


def test_cli_add_node_reads_prompt_from_stdin(db, monkeypatch):
    _diamond(db)
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("piped long prompt\n"))
    cg.cmd_graph_add_node(_args(db, prompt="-"))
    assert g.get_node(db, "x")["prompt"] == "piped long prompt"


def test_cli_add_node_unknown_dep_exits_nonzero(db, capsys):
    _diamond(db)
    with pytest.raises(SystemExit) as ei:
        cg.cmd_graph_add_node(_args(db, deps="ghost"))
    assert ei.value.code != 0
    assert g.get_node(db, "x") is None  # nothing written
    assert "REFUSED" in capsys.readouterr().err


def test_cli_add_node_json_output(db, capsys):
    _diamond(db)
    cg.cmd_graph_add_node(_args(db, deps=None, json_out=True))
    import json
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["node_id"] == "x"
    assert payload["state"] == "ready"


def test_cli_add_node_unknown_project_exits(db):
    with pytest.raises(SystemExit):
        cg.cmd_graph_add_node(_args(db, project="NOPE"))
