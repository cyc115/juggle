"""Autopilot Phase 3 — failure propagation (design rev 2).

Any node entering failed-exec|failed-integration|failed-verify blocks ALL its
transitive dependents (→ 'blocked-failed') with an action item naming them;
siblings on other branches are unaffected; the dispatcher tick must never
claim or dispatch a blocked-failed node.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from juggle_db import JuggleDB
from dbops import db_graph as g
import juggle_graph_dispatch as gd


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


def _load_tree(db):
    """a → b → c → d (chain) plus a → s (sibling branch off the root)."""
    for nid, title in [("a", "Root"), ("b", "Mid"), ("c", "Child"),
                       ("d", "Grandchild"), ("s", "Sibling")]:
        g.create_node(db, node_id=nid, project_id="INBOX", title=title,
                      prompt=f"do {nid}")
    g.replace_edges(db, "b", ["a"])
    g.replace_edges(db, "c", ["b"])
    g.replace_edges(db, "d", ["c"])
    g.replace_edges(db, "s", ["a"])
    g.recompute_ready(db, "INBOX")


def _states(db):
    return {n["id"]: n["state"] for n in g.list_nodes(db, "INBOX")}


def _fail_node(db, nid, terminal_event):
    """Walk a node legally to its failed-* state without propagation."""
    walk = {"pending": ["deps_ready"], "ready": []}[g.get_node(db, nid)["state"]]
    for ev in walk + ["claim", "dispatch"]:
        g.node_transition(db, nid, ev)
    if terminal_event == "exec_fail":
        g.node_transition(db, nid, "exec_fail")
    else:
        g.node_transition(db, nid, "integrate_start")
        g.node_transition(db, nid, terminal_event)


# ── propagate_failure unit ─────────────────────────────────────────────────────

def test_propagate_failure_blocks_transitive_dependents_pin(db):
    """Regression pin (2026-06-10): through Phase 2, dependents of a failed
    node were left silently 'pending' forever — the graph stalled with no
    blocked marker. propagate_failure must walk ALL transitive dependents
    (c AND d, not just direct c) to 'blocked-failed', leaving the sibling
    branch (s) untouched."""
    _load_tree(db)
    _fail_node(db, "b", "exec_fail")

    blocked = g.propagate_failure(db, "b")

    assert blocked == ["c", "d"]
    states = _states(db)
    assert states["b"] == "failed-exec"
    assert states["c"] == "blocked-failed"
    assert states["d"] == "blocked-failed"
    assert states["s"] == "pending", "sibling branch must be unaffected"


def test_propagate_failure_idempotent_and_diamond_safe(db):
    """A dependent reachable via two failed/blocked paths is blocked exactly
    once; re-propagation is a no-op (returns [])."""
    g.create_node(db, node_id="x", project_id="INBOX", title="X", prompt="x")
    g.create_node(db, node_id="l", project_id="INBOX", title="L", prompt="l")
    g.create_node(db, node_id="r", project_id="INBOX", title="R", prompt="r")
    g.create_node(db, node_id="j", project_id="INBOX", title="J", prompt="j")
    g.replace_edges(db, "l", ["x"])
    g.replace_edges(db, "r", ["x"])
    g.replace_edges(db, "j", ["l", "r"])
    g.recompute_ready(db, "INBOX")
    _fail_node(db, "x", "integrate_fail")

    assert sorted(g.propagate_failure(db, "x")) == ["j", "l", "r"]
    assert g.propagate_failure(db, "x") == []
    assert {_states(db)[n] for n in "lrj"} == {"blocked-failed"}


def test_propagate_failure_blocks_ready_dependents_too(db):
    """A dependent already promoted to 'ready' (e.g. via a racing recompute)
    is blocked as well, not just 'pending' ones."""
    g.create_node(db, node_id="p", project_id="INBOX", title="P", prompt="p")
    g.create_node(db, node_id="q", project_id="INBOX", title="Q", prompt="q")
    g.replace_edges(db, "q", ["p"])
    g.recompute_ready(db, "INBOX")
    _fail_node(db, "p", "verify_fail")
    # force q to 'ready' (test-only direct write; deps_ready is illegal here)
    with db._connect() as conn:
        conn.execute("UPDATE graph_nodes SET state='ready' WHERE id='q'")
        conn.commit()

    assert g.propagate_failure(db, "p") == ["q"]
    assert _states(db)["q"] == "blocked-failed"


# ── completion-path propagation (mark_graph_node) ──────────────────────────────

def _bind_thread(db, nid):
    tid = db.create_thread(f"node {nid}", session_id="sessF")
    g.set_node_thread(db, nid, tid)
    return tid


def test_failed_integration_completion_blocks_dependents_pin(db):
    """Regression pin (2026-06-10): completing a mid-graph node with a failed
    integrate left its dependents silently pending (no blocked-failed, no
    action item naming them) — the armed graph stalled forever. The completion
    path must propagate and the action item must name the blocked nodes."""
    _load_tree(db)
    from juggle_cmd_agents_graph import mark_graph_node

    tid = _bind_thread(db, "b")
    for ev in ("deps_ready", "claim", "dispatch"):
        g.node_transition(db, "b", ev)
    mark_graph_node(db, tid, False, "b attempted", "sessF")

    states = _states(db)
    assert states["b"] == "failed-integration"
    assert states["c"] == "blocked-failed"
    assert states["d"] == "blocked-failed"
    assert states["s"] == "pending"
    items = db.get_open_action_items()
    assert any("blocked" in i["message"].lower() and "c" in i["message"]
               and "d" in i["message"] and i["priority"] == "high"
               for i in items), f"no action item names the blocked nodes: {items}"


def test_failed_verify_completion_blocks_dependents(db):
    """The failed-verify exit (DA M3 channel) propagates identically."""
    _load_tree(db)
    from juggle_cmd_agents_graph import mark_graph_node

    tid = _bind_thread(db, "b")
    for ev in ("deps_ready", "claim", "dispatch"):
        g.node_transition(db, "b", ev)
    mark_graph_node(db, tid, False, "b attempted", "sessF", verify_failed=True)

    states = _states(db)
    assert states["b"] == "failed-verify"
    assert states["c"] == "blocked-failed"
    assert states["d"] == "blocked-failed"


def test_verified_completion_does_not_propagate(db):
    _load_tree(db)
    from juggle_cmd_agents_graph import mark_graph_node

    tid = _bind_thread(db, "a")  # _load_tree already promoted a to ready
    mark_graph_node(db, tid, True, "a done", "sessF")

    states = _states(db)
    assert states["a"] == "verified"
    assert states["b"] == "ready" and states["s"] == "ready"
    assert "blocked-failed" not in states.values()


# ── dispatcher never touches blocked-failed ────────────────────────────────────

def test_tick_never_claims_or_dispatches_blocked_failed_pin(db):
    """Regression pin (2026-06-10): blocked-failed nodes are operator
    territory — the dispatch tick must never claim them (CAS only matches
    'ready'), never dispatch them, and never 'self-heal' them back to ready."""
    _load_tree(db)
    _fail_node(db, "b", "integrate_fail")
    g.propagate_failure(db, "b")
    db.set_setting(gd.ARMED_PROJECT_KEY, "INBOX")

    assert gd.claim_node(db, "c") is False, "claimed a blocked-failed node"

    dispatched = []
    stats = gd.graph_tick(
        db, dispatch_fn=lambda db_, tid, prompt, node: dispatched.append(node["id"])
    )
    blocked_set = {"c", "d"}
    assert blocked_set.isdisjoint(stats["dispatched"])
    assert blocked_set.isdisjoint(dispatched)
    assert _states(db)["c"] == "blocked-failed"
    assert _states(db)["d"] == "blocked-failed"
