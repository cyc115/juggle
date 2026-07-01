"""Autopilot Phase 3 — failure propagation (design rev 2).

Any task entering failed-exec|failed-integration|failed-verify blocks ALL its
transitive dependents (→ 'blocked-failed') with an action item naming them;
siblings on other branches are unaffected; the dispatcher tick must never
claim or dispatch a blocked-failed task.
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
        g.create_task(db, task_id=nid, project_id="INBOX", title=title,
                      prompt=f"do {nid}")
    g.replace_edges(db, "b", ["a"])
    g.replace_edges(db, "c", ["b"])
    g.replace_edges(db, "d", ["c"])
    g.replace_edges(db, "s", ["a"])
    g.recompute_ready(db, "INBOX")


def _states(db):
    return {n["id"]: n["state"] for n in g.list_tasks(db, "INBOX")}


def _fail_task(db, nid, terminal_event):
    """Walk a task legally to its failed-* state without propagation."""
    walk = {"open": ["deps_ready"], "ready": []}[g.get_task(db, nid)["state"]]
    for ev in walk + ["claim", "dispatch"]:
        g.task_transition(db, nid, ev)
    if terminal_event == "exec_fail":
        g.task_transition(db, nid, "exec_fail")
    else:
        g.task_transition(db, nid, "integrate_start")
        g.task_transition(db, nid, terminal_event)


# ── propagate_failure unit ─────────────────────────────────────────────────────

def test_propagate_failure_blocks_transitive_dependents_pin(db):
    """Regression pin (2026-06-10): through Phase 2, dependents of a failed
    task were left silently 'open' forever — the graph stalled with no
    blocked marker. propagate_failure must walk ALL transitive dependents
    (c AND d, not just direct c) to 'blocked-failed', leaving the sibling
    branch (s) untouched."""
    _load_tree(db)
    _fail_task(db, "b", "exec_fail")

    blocked = g.propagate_failure(db, "b")

    assert blocked == ["c", "d"]
    states = _states(db)
    assert states["b"] == "failed-exec"
    assert states["c"] == "blocked-failed"
    assert states["d"] == "blocked-failed"
    assert states["s"] == "open", "sibling branch must be unaffected"


def test_propagate_failure_idempotent_and_diamond_safe(db):
    """A dependent reachable via two failed/blocked paths is blocked exactly
    once; re-propagation is a no-op (returns [])."""
    g.create_task(db, task_id="x", project_id="INBOX", title="X", prompt="x")
    g.create_task(db, task_id="l", project_id="INBOX", title="L", prompt="l")
    g.create_task(db, task_id="r", project_id="INBOX", title="R", prompt="r")
    g.create_task(db, task_id="j", project_id="INBOX", title="J", prompt="j")
    g.replace_edges(db, "l", ["x"])
    g.replace_edges(db, "r", ["x"])
    g.replace_edges(db, "j", ["l", "r"])
    g.recompute_ready(db, "INBOX")
    _fail_task(db, "x", "integrate_fail")

    assert sorted(g.propagate_failure(db, "x")) == ["j", "l", "r"]
    assert g.propagate_failure(db, "x") == []
    assert {_states(db)[n] for n in "lrj"} == {"blocked-failed"}


def test_propagate_failure_blocks_ready_dependents_too(db):
    """A dependent already promoted to 'ready' (e.g. via a racing recompute)
    is blocked as well, not just 'open' ones."""
    g.create_task(db, task_id="p", project_id="INBOX", title="P", prompt="p")
    g.create_task(db, task_id="q", project_id="INBOX", title="Q", prompt="q")
    g.replace_edges(db, "q", ["p"])
    g.recompute_ready(db, "INBOX")
    _fail_task(db, "p", "verify_fail")
    # force q to 'ready' (test-only direct write; deps_ready is illegal here)
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET state='ready' WHERE id='q'")
        conn.commit()

    assert g.propagate_failure(db, "p") == ["q"]
    assert _states(db)["q"] == "blocked-failed"


# ── completion-path propagation (mark_graph_task) ──────────────────────────────

def _bind_thread(db, nid):
    tid = db.create_thread(f"task {nid}", session_id="sessF")
    g.set_task_thread(db, nid, tid)
    return tid


def test_failed_integration_completion_blocks_dependents_pin(db):
    """Regression pin (2026-06-10): completing a mid-graph task with a failed
    integrate left its dependents silently pending (no blocked-failed, no
    action item naming them) — the armed graph stalled forever. The completion
    path must propagate and the action item must name the blocked tasks."""
    _load_tree(db)
    from juggle_cmd_agents_graph import mark_graph_task

    tid = _bind_thread(db, "b")
    for ev in ("deps_ready", "claim", "dispatch"):
        g.task_transition(db, "b", ev)
    mark_graph_task(db, tid, False, "b attempted", "sessF")

    states = _states(db)
    assert states["b"] == "failed-integration"
    assert states["c"] == "blocked-failed"
    assert states["d"] == "blocked-failed"
    assert states["s"] == "open"
    items = db.get_open_action_items()
    assert any("blocked" in i["message"].lower() and "c" in i["message"]
               and "d" in i["message"] and i["priority"] == "high"
               for i in items), f"no action item names the blocked tasks: {items}"


def test_failed_verify_completion_blocks_dependents(db, monkeypatch):
    """The failed-verify exit (DA M3 channel) propagates identically.

    Fallback disabled (N=0) so this pins the TERMINAL propagation: with retries
    available the verify-fallback resets the task to ready instead (covered in
    test_verify_fallback)."""
    monkeypatch.setenv("JUGGLE_VERIFY_FALLBACK_RETRIES", "0")
    _load_tree(db)
    from juggle_cmd_agents_graph import mark_graph_task

    tid = _bind_thread(db, "b")
    for ev in ("deps_ready", "claim", "dispatch"):
        g.task_transition(db, "b", ev)
    mark_graph_task(db, tid, False, "b attempted", "sessF", verify_failed=True)

    states = _states(db)
    assert states["b"] == "failed-verify"
    assert states["c"] == "blocked-failed"
    assert states["d"] == "blocked-failed"


def test_verified_completion_does_not_propagate(db):
    _load_tree(db)
    from juggle_cmd_agents_graph import mark_graph_task

    tid = _bind_thread(db, "a")  # _load_tree already promoted a to ready
    mark_graph_task(db, tid, True, "a done", "sessF")

    states = _states(db)
    assert states["a"] == "verified"
    assert states["b"] == "ready" and states["s"] == "ready"
    assert "blocked-failed" not in states.values()


# ── dispatcher never touches blocked-failed ────────────────────────────────────

def test_tick_never_claims_or_dispatches_blocked_failed_pin(db):
    """Regression pin (2026-06-10): blocked-failed tasks are operator
    territory — the dispatch tick must never claim them (CAS only matches
    'ready'), never dispatch them, and never 'self-heal' them back to ready."""
    _load_tree(db)
    _fail_task(db, "b", "integrate_fail")
    g.propagate_failure(db, "b")
    db.set_setting(gd.ARMED_PROJECT_KEY, "INBOX")

    assert gd.claim_task(db, "c") is False, "claimed a blocked-failed task"

    dispatched = []
    stats = gd.graph_tick(
        db, dispatch_fn=lambda db_, tid, prompt, task: dispatched.append(task["id"])
    )
    blocked_set = {"c", "d"}
    assert blocked_set.isdisjoint(stats["dispatched"])
    assert blocked_set.isdisjoint(dispatched)
    assert _states(db)["c"] == "blocked-failed"
    assert _states(db)["d"] == "blocked-failed"
