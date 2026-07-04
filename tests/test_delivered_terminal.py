"""Phase-3 pins for the `delivered` success-terminal (loop-entity V1,
2026-07-04). A delivery='deliver' topic reaches a NEW terminal-SUCCESS state
`delivered` with NO merged_sha, while delivery='merge' keeps the EXISTING
verified⟺merged gate byte-for-byte. `delivered` must be accepted everywhere
`verified` is treated as terminal-success.

Incidents pinned:
  * LEAD FINDING (critique 2026-07-04): a deliver topic that can never go
    terminal wedges the loop iteration → §9 overlap-skip then skips every future
    fire forever = silent permanent loop death. The completion gate + the
    mark-completion seam must map deliver→delivered.
  * verified⟺merged invariant (graph_guards.topic_is_merged): a merge topic with
    no merged_sha is STILL refused. `delivered` must never forge that proof.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as t  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "delivered.db"))
    d.init_db()
    return d


def _topic(db, tid, project="INBOX"):
    t.create_topic(db, topic_id=tid, project_id=project, title=f"Topic {tid}")


def _task(db, nid, topic_id):
    g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt=f"do {nid}")
    g.set_task_topic(db, nid, topic_id)
    g.task_transition(db, nid, "deps_ready")
    g.task_transition(db, nid, "claim")
    g.task_transition(db, nid, "dispatch")
    g.task_transition(db, nid, "integrate_start")
    g.task_transition(db, nid, "integrate_ok")


def _set_delivery(db, tid, delivery):
    with db._connect() as c:
        c.execute("UPDATE nodes SET delivery=? WHERE id=? AND kind='topic'",
                  (delivery, tid))
        c.commit()


# ── The two load-bearing contract pins ──────────────────────────────────────────
def test_deliver_topic_reaches_delivered_not_wedged(db):
    """LOAD-BEARING (LEAD FINDING). A delivery='deliver' topic with NO branch/repo
    completes to 'delivered' and does NOT stall pre-verified. RED on pre-Phase-3
    code: mark_topic_completion walks integrate_ok→verified, the merged_sha gate
    refuses (UnmergedVerifyRefused), the topic wedges → the loop dies silently."""
    _topic(db, "D1")
    _task(db, "n1", "D1")
    _set_delivery(db, "D1", "deliver")

    state = t.mark_topic_completion(db, "D1", integrate_ok=True, verify_ok=True)

    assert state == "delivered"
    topic = t.get_topic(db, "D1")
    assert topic["state"] == "delivered"
    assert not topic.get("merged_sha")


def test_merge_topic_still_requires_merged_sha(db):
    """INVARIANT: delivery='merge' (the default) is UNCHANGED — a topic with no
    merged_sha is STILL refused verified (verified⟺merged, graph_guards.
    topic_is_merged). Adding 'delivered' must not relax this gate."""
    _topic(db, "M1")
    _task(db, "n2", "M1")

    with pytest.raises(t.UnmergedVerifyRefused):
        t.mark_topic_completion(db, "M1", integrate_ok=True, verify_ok=True)

    assert t.get_topic(db, "M1")["state"] == "integrating"


def test_delivered_never_sets_merged_sha(db):
    """A completed deliver topic leaves merged_sha NULL — deliver must not forge
    the merge proof."""
    _topic(db, "D2")
    _task(db, "n3", "D2")
    _set_delivery(db, "D2", "deliver")

    t.mark_topic_completion(db, "D2", integrate_ok=True, verify_ok=True)

    assert not t.get_topic(db, "D2").get("merged_sha")


# ── Terminal-success consumers must count `delivered` as done ────────────────────
def test_delivered_counts_as_terminal_success():
    """The real terminal-success consumers audited for Phase 3 all count a
    `delivered` node as done, so a delivered loop never reads perpetually
    N/M-incomplete. RED on pre-Phase-3 code (progress counts only 'verified';
    the canonical sets lack 'delivered')."""
    from dbops import terminal_states as ts
    from juggle_graph_status import counts_from_states, format_progress

    # progress % (juggle_graph_status) — the cockpit "3/14 done" numerator
    counts = counts_from_states(["delivered", "verified", "open"])
    assert counts["verified"] == 2  # both success-terminals count as done
    assert format_progress(counts).startswith("2/3 done")

    # reconcile topic-rollup completion set + topic-done derivation + completion gate
    assert "delivered" in ts.COMPLETION_TERMINAL_STATES  # db_topics_reconcile
    assert "delivered" in ts.MERGED_TERMINAL_STATES      # juggle_topic_derive
    assert "delivered" in ts.MERGED_DONE_STATES          # cockpit_tree glyph
    assert "delivered" in ts.TASK_TERMINAL_STATES        # completion gate
    assert "delivered" in ts.TERMINAL_STATES
    assert ts.is_success_terminal("delivered")
    # delivered is NOT in-flight (learnings-rollup inverse relies on this)
    assert "delivered" not in ts.IN_FLIGHT_STATES
    assert not ts.is_in_flight("delivered")


def test_completion_gate_accepts_delivered_task(db):
    """LEAD-FINDING guard: check_topic_completion_gate must NOT refuse a topic
    whose member task is 'delivered' (delivered ∈ TASK_TERMINAL_STATES). RED on
    pre-Phase-3 code: a task cannot even reach 'delivered' (no deliver_ok edge)."""
    from juggle_cmd_agents_graph_topics import check_topic_completion_gate

    _topic(db, "G1")
    g.create_task(db, task_id="gk", project_id="INBOX", title="gk", prompt="x")
    g.set_task_topic(db, "gk", "G1")
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start", "deliver_ok"):
        g.task_transition(db, "gk", ev)
    assert t.get_topic(db, "G1") is not None
    thread_id = db.create_thread(topic="w", session_id="s")
    t.set_topic_thread(db, "G1", thread_id)

    assert check_topic_completion_gate(db, thread_id) is None


# ── Node-machine: delivered is reachable and not a dead-end ──────────────────────
def test_node_machine_delivered_has_legal_exits():
    """`delivered` has an incoming deliver_ok edge and the same exits as verified
    (g1_pass→done, archive→archived, reopen→open) — never a dead-end state."""
    from dbops.db_node_machine import node_transition

    assert node_transition("integrating", "deliver_ok", "task") == "delivered"
    assert node_transition("delivered", "g1_pass", "task") == "done"
    assert node_transition("delivered", "archive", "task") == "archived"
    assert node_transition("delivered", "reopen", "task") == "open"


def test_deliver_completion_is_idempotent(db):
    """A racing double-completion on an already-'delivered' topic must not raise
    (mirrors the already-verified idempotency guard, Bug I 2026-06-11)."""
    _topic(db, "D3")
    _task(db, "n4", "D3")
    _set_delivery(db, "D3", "deliver")
    t.mark_topic_completion(db, "D3", integrate_ok=True, verify_ok=True)

    state = t.mark_topic_completion(db, "D3", integrate_ok=True, verify_ok=True)

    assert state == "delivered"
