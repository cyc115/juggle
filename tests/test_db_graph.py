"""Tests for dbops.db_graph — graph_nodes/graph_edges plan store (project autopilot Phase 1).

Covers: schema/migration, CRUD, the node state machine (every legal transition
plus fail-loud illegal ones), ready-set recompute, and completion marking
(DA B3: integrate failure must never yield 'verified'). Cycle detection moved
to juggle_cmd_graph (load-time validation) — see test_cmd_graph.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


def _mk(db, node_id, deps=(), project_id="INBOX", **kw):
    g.create_node(
        db,
        node_id=node_id,
        project_id=project_id,
        title=kw.get("title", node_id),
        prompt=kw.get("prompt", f"do {node_id}"),
        verify_cmd=kw.get("verify_cmd"),
    )
    if deps:
        g.replace_edges(db, node_id, list(deps))


# ── schema ─────────────────────────────────────────────────────────────────────


def test_init_db_creates_graph_tables(db):
    with db._connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "graph_nodes" in tables
    assert "graph_edges" in tables


def test_migration_adds_graph_tables_to_existing_db(tmp_path):
    """Migration 35: pre-existing DB without graph tables gains them on init_db."""
    import sqlite3

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, topic TEXT)")
    conn.commit()
    conn.close()
    d = JuggleDB(db_path=str(db_path))
    d.init_db()
    with d._connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"graph_nodes", "graph_edges"} <= tables


# ── CRUD ───────────────────────────────────────────────────────────────────────


def test_create_and_get_node(db):
    _mk(db, "n1", title="Node One", prompt="build it", verify_cmd="pytest -q")
    node = g.get_node(db, "n1")
    assert node["id"] == "n1"
    assert node["title"] == "Node One"
    assert node["prompt"] == "build it"
    assert node["verify_cmd"] == "pytest -q"
    assert node["state"] == "pending"
    assert node["thread_id"] is None
    assert node["verified_at"] is None


def test_get_node_missing_returns_none(db):
    assert g.get_node(db, "nope") is None


def test_duplicate_create_raises(db):
    _mk(db, "n1")
    with pytest.raises(Exception):
        g.create_node(db, node_id="n1", project_id="INBOX", title="x", prompt="y")


def test_list_nodes_scoped_to_project(db):
    p1 = db.create_project("P1", objective="test")
    _mk(db, "a", project_id="INBOX")
    _mk(db, "b", project_id=p1)
    assert [n["id"] for n in g.list_nodes(db, "INBOX")] == ["a"]
    assert [n["id"] for n in g.list_nodes(db, p1)] == ["b"]


def test_edges_replace_and_get(db):
    _mk(db, "a")
    _mk(db, "b")
    _mk(db, "c", deps=["a", "b"])
    assert sorted(g.get_deps(db, "c")) == ["a", "b"]
    g.replace_edges(db, "c", ["a"])
    assert g.get_deps(db, "c") == ["a"]


def test_set_thread_and_handoff_do_not_touch_state(db):
    _mk(db, "n1")
    g.set_node_thread(db, "n1", "thread-uuid")
    g.set_node_handoff(db, "n1", '{"files": ["x.py"]}')
    node = g.get_node(db, "n1")
    assert node["thread_id"] == "thread-uuid"
    assert node["handoff"] == '{"files": ["x.py"]}'
    assert node["state"] == "pending"


def test_get_node_by_thread(db):
    _mk(db, "n1")
    g.set_node_thread(db, "n1", "t-123")
    assert g.get_node_by_thread(db, "t-123")["id"] == "n1"
    assert g.get_node_by_thread(db, "t-999") is None


# ── state machine ──────────────────────────────────────────────────────────────

LEGAL_CHAIN = [
    ("deps_ready", "ready"),
    ("claim", "dispatching"),
    ("dispatch", "running"),
    ("integrate_start", "integrating"),
    ("integrate_ok", "verified"),
]


def test_full_legal_happy_path(db):
    _mk(db, "n1")
    for event, expected in LEGAL_CHAIN:
        assert g.node_transition(db, "n1", event) == expected
    node = g.get_node(db, "n1")
    assert node["state"] == "verified"
    assert node["verified_at"]  # stored, never inferred from thread status


@pytest.mark.parametrize(
    "path,event,expected",
    [
        (["deps_ready", "claim"], "stale_reset", "ready"),  # crash-safe sweep
        (["deps_ready", "claim", "dispatch"], "exec_fail", "failed-exec"),
        (
            ["deps_ready", "claim", "dispatch", "integrate_start"],
            "integrate_fail",
            "failed-integration",
        ),
        (
            ["deps_ready", "claim", "dispatch", "integrate_start"],
            "verify_fail",
            "failed-verify",
        ),
        ([], "dep_fail", "blocked-failed"),
        (["deps_ready"], "dep_fail", "blocked-failed"),
        # DA round-2 BLOCKER-1 (2026-06-10): blocked-failed had NO outgoing
        # transition — reload of a fixed spec left the blocked tail dead forever.
        (["dep_fail"], "reload", "pending"),
        (["deps_ready", "dep_fail"], "reload", "pending"),
    ],
)
def test_legal_failure_and_reset_transitions(db, path, event, expected):
    _mk(db, "n1")
    for ev in path:
        g.node_transition(db, "n1", ev)
    assert g.node_transition(db, "n1", event) == expected
    assert g.get_node(db, "n1")["state"] == expected


@pytest.mark.parametrize(
    "path,bad_event",
    [
        ([], "claim"),  # pending cannot be claimed
        ([], "integrate_ok"),  # pending cannot complete
        (["deps_ready"], "dispatch"),  # ready cannot skip claim
        (["deps_ready", "claim", "dispatch", "integrate_start", "integrate_ok"], "claim"),
        (["deps_ready", "claim", "dispatch", "integrate_start", "integrate_ok"], "deps_ready"),
        (["deps_ready", "claim", "dispatch", "exec_fail"], "integrate_ok"),
        # blocked-failed resumes ONLY via 'reload' (DA round-2 BLOCKER-1,
        # 2026-06-10) — direct promotion stays illegal:
        (["dep_fail"], "deps_ready"),
    ],
)
def test_illegal_transitions_fail_loud(db, path, bad_event):
    _mk(db, "n1")
    for ev in path:
        g.node_transition(db, "n1", ev)
    before = g.get_node(db, "n1")["state"]
    with pytest.raises(ValueError):
        g.node_transition(db, "n1", bad_event)
    assert g.get_node(db, "n1")["state"] == before  # state untouched on error


def test_unknown_event_fails_loud(db):
    _mk(db, "n1")
    with pytest.raises(ValueError):
        g.node_transition(db, "n1", "bogus_event")


def test_transition_missing_node_fails_loud(db):
    with pytest.raises(ValueError):
        g.node_transition(db, "ghost", "deps_ready")


# ── ready set ──────────────────────────────────────────────────────────────────


def test_recompute_ready_promotes_root_nodes(db):
    _mk(db, "a")
    _mk(db, "b", deps=["a"])
    newly = g.recompute_ready(db, "INBOX")
    assert newly == ["a"]
    assert g.get_node(db, "a")["state"] == "ready"
    assert g.get_node(db, "b")["state"] == "pending"


def test_recompute_ready_requires_all_deps_verified(db):
    _mk(db, "a")
    _mk(db, "b")
    _mk(db, "c", deps=["a", "b"])
    g.recompute_ready(db, "INBOX")
    # verify a only
    for ev in ("claim", "dispatch", "integrate_start", "integrate_ok"):
        g.node_transition(db, "a", ev)
    assert g.recompute_ready(db, "INBOX") == []
    assert g.get_node(db, "c")["state"] == "pending"
    # verify b → c becomes ready
    for ev in ("claim", "dispatch", "integrate_start", "integrate_ok"):
        g.node_transition(db, "b", ev)
    assert g.recompute_ready(db, "INBOX") == ["c"]
    assert g.get_node(db, "c")["state"] == "ready"


def test_recompute_ready_idempotent(db):
    _mk(db, "a")
    assert g.recompute_ready(db, "INBOX") == ["a"]
    assert g.recompute_ready(db, "INBOX") == []


def test_unverified_deps_listing(db):
    _mk(db, "a")
    _mk(db, "b")
    _mk(db, "c", deps=["a", "b"])
    assert sorted(g.unverified_deps(db, "c")) == ["a", "b"]
    g.recompute_ready(db, "INBOX")
    for ev in ("claim", "dispatch", "integrate_start", "integrate_ok"):
        g.node_transition(db, "a", ev)
    assert g.unverified_deps(db, "c") == ["b"]


# ── completion marking ─────────────────────────────────────────────────────────


def test_mark_completion_success_yields_verified(db):
    _mk(db, "n1")
    g.recompute_ready(db, "INBOX")
    state = g.mark_completion(db, "n1", integrate_ok=True, handoff="did the thing")
    assert state == "verified"
    node = g.get_node(db, "n1")
    assert node["handoff"] == "did the thing"
    assert node["verified_at"]


def test_mark_completion_integrate_failure_never_verified(db):
    """REQUIRED PIN (DA B3, 2026-06-10 design): cmd_complete_agent closes the
    thread even when integrate FAILS; node truth must record failed-integration,
    NEVER 'verified'."""
    _mk(db, "n1")
    g.recompute_ready(db, "INBOX")
    state = g.mark_completion(db, "n1", integrate_ok=False)
    assert state == "failed-integration"
    node = g.get_node(db, "n1")
    assert node["state"] != "verified"
    assert node["verified_at"] is None


def test_mark_completion_verify_failure_yields_failed_verify(db):
    _mk(db, "n1")
    g.recompute_ready(db, "INBOX")
    assert g.mark_completion(db, "n1", integrate_ok=True, verify_ok=False) == "failed-verify"


def test_mark_completion_on_terminal_node_fails_loud(db):
    _mk(db, "n1")
    g.recompute_ready(db, "INBOX")
    g.mark_completion(db, "n1", integrate_ok=True)
    with pytest.raises(ValueError):
        g.mark_completion(db, "n1", integrate_ok=True)


# ── reload hygiene (DA round-2, 2026-06-10) ───────────────────────────────────


def test_reload_clears_stale_thread_binding(db):
    """REGRESSION PIN (DA round-2 minor 4, 2026-06-10): 'reload' of a failed
    node kept the dead thread's id on the node — get_node_by_thread and the
    [blocked:]/[ready] context tags kept resolving to a closed thread, and a
    later completion of that zombie thread could re-mark the resurrected node.
    Reload must clear thread_id."""
    _mk(db, "n1")
    g.recompute_ready(db, "INBOX")
    for ev in ("claim", "dispatch", "exec_fail"):
        g.node_transition(db, "n1", ev)
    g.set_node_thread(db, "n1", "dead-thread-uuid")
    assert g.node_transition(db, "n1", "reload") == "pending"
    assert g.get_node(db, "n1")["thread_id"] is None
