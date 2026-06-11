"""Integration test: 4-node diamond graph driven end-to-end by real
cmd_complete_agent calls + directly-invoked graph_tick (autopilot Phase 2).

FakeMgr pattern: tmp DB, fake dispatch_fn, no tmux, no LLM. Asserts the full
pending→ready→dispatching→running→verified flow with ready-set propagation,
plus the failed-integration branch (Phase 3 semantics: dependents of a failed
node go 'blocked-failed', siblings are unaffected, blocked nodes are never
dispatched, and an action item names them — no silent stall).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest  # noqa: E402

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
import juggle_graph_dispatch as gd  # noqa: E402

# 2026-06-11 (R9 Task 6): graph_tick now claims/dispatches TOPICS, not flat
# nodes, so this node-level diamond e2e flow is obsolete as a product path. Its
# topic-level replacement needs the topic-completion + integrate-once + failure
# action-item wiring built in Task 7 (mark-task / complete-agent topic gate).
# Deferred (xfail, approved 2026-06-11) — Task 7 rewrites these to the topic
# seam in test_graph_contract.py and restores the pins. strict=False: a future
# accidental pass must not itself fail the suite before Task 7 lands.
_R9_TASK7 = pytest.mark.xfail(
    reason="node-level autopilot e2e obsoleted by R9 topic tick (Task 6); "
    "restore at topic level in Task 7 (topic completion/integrate wiring)",
    strict=False,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    d._set_session_key_external("session_id", "sessI")
    import juggle_cli_common as common
    import juggle_cmd_agents_common as agents_common

    monkeypatch.setattr(common, "get_db", lambda: d)
    monkeypatch.setattr(agents_common, "get_db", lambda: d)
    return d


def _load_diamond(db):
    """a → (b, c) → d."""
    g.create_node(db, node_id="a", project_id="INBOX", title="Base", prompt="build base")
    g.create_node(db, node_id="b", project_id="INBOX", title="Left", prompt="build left")
    g.create_node(db, node_id="c", project_id="INBOX", title="Right", prompt="build right")
    g.create_node(db, node_id="d", project_id="INBOX", title="Join", prompt="join sides")
    g.replace_edges(db, "b", ["a"])
    g.replace_edges(db, "c", ["a"])
    g.replace_edges(db, "d", ["b", "c"])
    g.recompute_ready(db, "INBOX")
    db.set_setting(gd.ARMED_PROJECT_KEY, "INBOX")


class FakeDispatch:
    def __init__(self):
        self.prompts: dict[str, str] = {}  # node_id → hydrated prompt

    def __call__(self, db, thread_id, prompt, node):
        self.prompts[node["id"]] = prompt


def _states(db):
    return {n["id"]: n["state"] for n in g.list_nodes(db, "INBOX")}


def _complete_node(db, node_id, handoff):
    """Complete the node's bound thread through the REAL cmd_complete_agent."""
    from juggle_cmd_agents import cmd_complete_agent

    node = g.get_node(db, node_id)
    assert node["thread_id"], f"node {node_id} has no bound thread"
    cmd_complete_agent(
        argparse.Namespace(
            thread_id=node["thread_id"],
            result_summary=f"{node_id} complete",
            retain_text=None,
            open_questions=None,
            handoff=handoff,
        )
    )


@_R9_TASK7
def test_diamond_happy_path_full_flow(db):
    _load_diamond(db)
    fake = FakeDispatch()

    # Tick 1: only the root is ready → dispatched
    stats = gd.graph_tick(db, dispatch_fn=fake)
    assert stats["dispatched"] == ["a"]
    assert _states(db) == {"a": "running", "b": "pending", "c": "pending", "d": "pending"}

    # Completing a (with handoff) promotes b and c; complete-agent NEVER dispatches
    _complete_node(db, "a", handoff="base API: use foo() from base.py")
    assert _states(db) == {"a": "verified", "b": "ready", "c": "ready", "d": "pending"}
    assert set(fake.prompts) == {"a"}  # no dispatch outside the tick

    # Tick 2: fan-out — b and c dispatched in one tick, hydrated from a's handoff
    stats = gd.graph_tick(db, dispatch_fn=fake)
    assert sorted(stats["dispatched"]) == ["b", "c"]
    assert _states(db)["b"] == "running" and _states(db)["c"] == "running"
    assert "base API: use foo() from base.py" in fake.prompts["b"]
    assert "base API: use foo() from base.py" in fake.prompts["c"]

    # Fan-in: d waits for BOTH
    _complete_node(db, "b", handoff="left exposes bar()")
    assert _states(db)["d"] == "pending"
    assert gd.graph_tick(db, dispatch_fn=fake)["dispatched"] == []
    _complete_node(db, "c", handoff="right exposes baz()")
    assert _states(db)["d"] == "ready"

    # Tick 3: d dispatched with BOTH upstream handoffs; leaf completes sans handoff
    stats = gd.graph_tick(db, dispatch_fn=fake)
    assert stats["dispatched"] == ["d"]
    assert "left exposes bar()" in fake.prompts["d"]
    assert "right exposes baz()" in fake.prompts["d"]
    _complete_node(db, "d", handoff=None)  # no dependents — contract not required

    assert _states(db) == {n: "verified" for n in "abcd"}
    assert all(g.get_node(db, n)["verified_at"] for n in "abcd")
    # every node ran on its own thread, all bound + project-tagged
    tids = {g.get_node(db, n)["thread_id"] for n in "abcd"}
    assert len(tids) == 4
    assert all(db.get_thread(t)["project_id"] == "INBOX" for t in tids)


@_R9_TASK7
def test_diamond_failed_integration_branch_blocks_dependents_loudly(db, monkeypatch):
    """b's integrate fails mid-diamond → b = failed-integration (DA B3: never
    verified), downstream d goes 'blocked-failed' and is NEVER dispatched,
    sibling c is unaffected (still verifies), and a HIGH action item flags the
    failure naming the blocked node (no silent stall — Phase 3, 2026-06-10;
    rewritten from the Phase 1/2 pin that asserted d merely stayed pending)."""
    import juggle_cmd_agents_common as _com

    _load_diamond(db)
    fake = FakeDispatch()
    gd.graph_tick(db, dispatch_fn=fake)
    _complete_node(db, "a", handoff="base done")
    gd.graph_tick(db, dispatch_fn=fake)

    # b's worktree integration fails; c (no worktree fields) finalizes fine —
    # _run_integrate only runs for threads with worktree fields, i.e. only b.
    b_tid = g.get_node(db, "b")["thread_id"]
    db.update_thread(b_tid, worktree_path="/tmp/wt-b", worktree_branch="cyc_b",
                     main_repo_path="/tmp/repo")
    monkeypatch.setattr(
        _com.juggle_cmd_integrate, "_run_integrate",
        lambda thread, db_: (False, "rebase conflict"),
    )
    _complete_node(db, "b", handoff="b attempted")
    _complete_node(db, "c", handoff="c done")

    states = _states(db)
    assert states["b"] == "failed-integration"
    assert states["c"] == "verified", "sibling must be unaffected by b's failure"
    assert states["d"] == "blocked-failed"  # Phase 3: propagated, not stalled

    # further ticks never dispatch d while it is blocked-failed
    stats = gd.graph_tick(db, dispatch_fn=fake)
    assert stats["dispatched"] == []
    assert "d" not in fake.prompts
    assert _states(db)["d"] == "blocked-failed"  # tick must not resurrect it
    items = db.get_open_action_items()
    assert any("failed-integration" in i["message"] and i["priority"] == "high"
               for i in items)
    assert any("blocked" in i["message"].lower() and "d" in i["message"]
               for i in items), "action item must name the blocked dependent"


@_R9_TASK7
def test_add_node_mid_execution_does_not_touch_running_node(db):
    """FEATURE PIN (graph add-node, 2026-06-10): injecting a node while another
    node is RUNNING must leave the running node untouched and execute the new
    node in dependency order via the tick (no manual dispatch)."""
    from types import SimpleNamespace
    import juggle_cmd_graph as cg

    _load_diamond(db)
    fake = FakeDispatch()

    # Tick 1: root a dispatched → running
    gd.graph_tick(db, dispatch_fn=fake)
    assert _states(db)["a"] == "running"
    a_node_before = g.get_node(db, "a")

    # Inject a new leaf 'x' depending on the still-running 'a', via the CLI.
    cg.cmd_graph_add_node(SimpleNamespace(
        project="INBOX", id="x", title="X", prompt="run after a",
        deps="a", required_by=None, verify_cmd=None, json_out=False,
        db_path=str(db.db_path),
    ))
    # running node a is byte-identical (state, thread, content)
    a_after = g.get_node(db, "a")
    assert a_after["state"] == "running"
    assert a_after["thread_id"] == a_node_before["thread_id"]
    assert a_after["prompt"] == a_node_before["prompt"]
    # x waits on a (a not verified yet) → pending, NOT dispatched this tick
    assert _states(db)["x"] == "pending"
    assert gd.graph_tick(db, dispatch_fn=fake)["dispatched"] == []
    assert "x" not in fake.prompts

    # complete a → x (and b, c) become ready; tick dispatches them
    _complete_node(db, "a", handoff="a done")
    assert _states(db)["x"] == "ready"
    dispatched = gd.graph_tick(db, dispatch_fn=fake)["dispatched"]
    assert "x" in dispatched
    assert _states(db)["x"] == "running"
