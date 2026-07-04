"""TDD tests for the pure Plan-view wave-layout engine
(cockpit-plan-view, 2026-07-02). juggle_plan_layout turns a project snapshot
(topic nodes + topic-dep edges + free-agent-slot count) into deterministic
dependency-wave columns: verified topics collapse to one anchor stub, open
topics layer by longest-path depth, ordered by barycenter, with the longest
unfinished chain flagged as the critical path. Pure — no Textual, no DB.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cockpit_graph_layout import GraphTask  # noqa: E402
from juggle_plan_layout import build_plan_layout  # noqa: E402


def _n(task_id, state="open", title="", tasks_done=None, tasks_total=None):
    return GraphTask(
        id=task_id, title=title or task_id, state=state,
        tasks_done=tasks_done, tasks_total=tasks_total,
    )


# ---------------------------------------------------------------------------
# Anchor collapse
# ---------------------------------------------------------------------------


def test_all_verified_collapses_to_anchor_only():
    tasks = [_n("A", state="verified"), _n("B", state="verified")]
    layout = build_plan_layout(tasks, edges=[], free_slots=3)
    assert layout.anchor_count == 2
    assert layout.waves == ()
    assert layout.critical_path == ()
    assert layout.edges == ()


def test_cancelled_topics_are_excluded_like_verified():
    """A cancelled node is a COMPLETION terminal — never an actionable card. It
    vanishes from the waves (kept in sync with the railroad's DONE_STATES) and,
    unlike verified, is NOT counted in the anchor stub."""
    tasks = [
        _n("A", state="ready"), _n("X", state="cancelled"),
        _n("V", state="verified"),
    ]
    layout = build_plan_layout(tasks, edges=[], free_slots=2)
    assert layout.anchor_count == 1  # only V (verified), not the cancelled X
    ids = [c.id for w in layout.waves for c in w.cards]
    assert ids == ["A"]  # cancelled X is not an open card


def test_verified_deps_do_not_extend_open_topic_waves():
    """A depends on verified V; V is not in the open graph so A is wave 0."""
    tasks = [_n("V", state="verified"), _n("A", state="ready")]
    edges = [("A", "V")]
    layout = build_plan_layout(tasks, edges=edges, free_slots=2)
    assert layout.anchor_count == 1
    assert len(layout.waves) == 1
    assert layout.waves[0].index == 0
    assert [c.id for c in layout.waves[0].cards] == ["A"]
    assert layout.edges == ()  # edge to a collapsed (verified) dep is dropped


# ---------------------------------------------------------------------------
# Wave (rank) assignment over the open subgraph — diamond
# ---------------------------------------------------------------------------


def test_diamond_waves_by_longest_path():
    """A -> {B,C} -> D: D's wave is 2 (longest path), not 1."""
    tasks = [_n("A", state="ready"), _n("B", state="open"),
              _n("C", state="open"), _n("D", state="open")]
    edges = [("B", "A"), ("C", "A"), ("D", "B"), ("D", "C")]
    layout = build_plan_layout(tasks, edges=edges, free_slots=1)
    wave_of = {c.id: w.index for w in layout.waves for c in w.cards}
    assert wave_of == {"A": 0, "B": 1, "C": 1, "D": 2}
    assert len(layout.waves) == 3


def test_diamond_critical_path_and_edge_flags():
    tasks = [_n("A", state="ready"), _n("B", state="open"),
              _n("C", state="open"), _n("D", state="open")]
    edges = [("B", "A"), ("C", "A"), ("D", "B"), ("D", "C")]
    layout = build_plan_layout(tasks, edges=edges, free_slots=1)
    # Both B and C tie at rank 1; deterministic pick is the lexicographically
    # smallest id, so the critical path runs A -> B -> D.
    assert layout.critical_path == ("A", "B", "D")
    on_path = {(e.dst, e.src) for e in layout.edges if e.on_critical_path}
    assert on_path == {("A", "B"), ("B", "D")}


def test_diamond_card_kinds_and_blocked_on():
    tasks = [_n("A", state="ready"), _n("B", state="open"),
              _n("C", state="open"), _n("D", state="open")]
    edges = [("B", "A"), ("C", "A"), ("D", "B"), ("D", "C")]
    layout = build_plan_layout(tasks, edges=edges, free_slots=1)
    cards_by_id = {c.id: c for w in layout.waves for c in w.cards}
    assert cards_by_id["A"].kind == "ready"
    assert cards_by_id["A"].blocked_on == ()
    assert cards_by_id["B"].kind == "blocked"
    assert cards_by_id["B"].blocked_on == ("A",)
    assert cards_by_id["D"].kind == "blocked"
    assert cards_by_id["D"].blocked_on == ("B", "C")


def test_running_state_overrides_blocked_kind():
    tasks = [_n("A", state="ready"), _n("B", state="running")]
    edges = [("B", "A")]
    layout = build_plan_layout(tasks, edges=edges, free_slots=1)
    cards_by_id = {c.id: c for w in layout.waves for c in w.cards}
    assert cards_by_id["B"].kind == "running"


def test_failed_state_maps_to_failed_kind():
    tasks = [_n("A", state="ready"), _n("B", state="failed-verify")]
    edges = [("B", "A")]
    layout = build_plan_layout(tasks, edges=edges, free_slots=1)
    cards_by_id = {c.id: c for w in layout.waves for c in w.cards}
    assert cards_by_id["B"].kind == "failed"


# ---------------------------------------------------------------------------
# Wave-0 parallel count vs free slots
# ---------------------------------------------------------------------------


def test_wave_zero_reports_parallel_vs_free_slots():
    tasks = [_n(f"T{i}", state="ready") for i in range(4)]
    layout = build_plan_layout(tasks, edges=[], free_slots=3)
    w0 = layout.waves[0]
    assert w0.parallel_count == 4
    assert w0.free_slots == 3
    assert w0.queued == 1


def test_non_zero_waves_have_no_slot_budget():
    tasks = [_n("A", state="ready"), _n("B", state="open")]
    edges = [("B", "A")]
    layout = build_plan_layout(tasks, edges=edges, free_slots=5)
    w1 = layout.waves[1]
    assert w1.free_slots is None
    assert w1.queued is None


# ---------------------------------------------------------------------------
# Fan-out (one dep, many dependents at the same wave) — barycenter order
# ---------------------------------------------------------------------------


def test_fan_out_orders_wave_by_barycenter_then_id():
    """A -> {X,Y,Z} all at wave 1; with only one predecessor each, ties break
    by id, giving a stable alphabetical order."""
    tasks = [_n("A", state="ready"), _n("X", state="open"),
              _n("Y", state="open"), _n("Z", state="open")]
    edges = [("X", "A"), ("Y", "A"), ("Z", "A")]
    layout = build_plan_layout(tasks, edges=edges, free_slots=1)
    assert [c.id for c in layout.waves[1].cards] == ["X", "Y", "Z"]


# ---------------------------------------------------------------------------
# Task-count passthrough
# ---------------------------------------------------------------------------


def test_task_counts_pass_through_to_cards():
    tasks = [_n("A", state="ready", tasks_done=2, tasks_total=5)]
    layout = build_plan_layout(tasks, edges=[], free_slots=1)
    card = layout.waves[0].cards[0]
    assert card.tasks_done == 2
    assert card.tasks_total == 5


# ---------------------------------------------------------------------------
# Determinism — same input always produces the same output
# ---------------------------------------------------------------------------


def test_layout_is_deterministic_across_calls():
    tasks = [_n(f"T{i}", state="ready" if i == 0 else "open") for i in range(6)]
    edges = [(f"T{i}", f"T{i - 1}") for i in range(1, 6)]
    a = build_plan_layout(tasks, edges=edges, free_slots=2)
    b = build_plan_layout(tasks, edges=edges, free_slots=2)
    assert a == b


# ---------------------------------------------------------------------------
# Golden: synthetic 5-node chain
# ---------------------------------------------------------------------------


def test_golden_chain_5_nodes():
    tasks = [_n(f"T{i}", state="ready" if i == 0 else "open") for i in range(5)]
    edges = [(f"T{i}", f"T{i - 1}") for i in range(1, 5)]
    layout = build_plan_layout(tasks, edges=edges, free_slots=1)
    assert [w.index for w in layout.waves] == [0, 1, 2, 3, 4]
    assert [[c.id for c in w.cards] for w in layout.waves] == [
        ["T0"], ["T1"], ["T2"], ["T3"], ["T4"],
    ]
    assert layout.critical_path == ("T0", "T1", "T2", "T3", "T4")
    assert len(layout.edges) == 4
    assert all(e.on_critical_path for e in layout.edges)


# ---------------------------------------------------------------------------
# Golden: synthetic 20-node fan-out DAG (4 waves of 5)
# ---------------------------------------------------------------------------


def _layered_fanout(n_waves: int, per_wave: int):
    """wave k depends on ALL nodes of wave k-1 (dense fan-in/out) — deterministic
    naming Wk_i so golden assertions are legible."""
    tasks = []
    edges = []
    for w in range(n_waves):
        for i in range(per_wave):
            tid = f"W{w}_{i}"
            tasks.append(_n(tid, state="ready" if w == 0 else "open"))
            if w > 0:
                for j in range(per_wave):
                    edges.append((tid, f"W{w - 1}_{j}"))
    return tasks, edges


def test_golden_layered_fanout_20_nodes():
    tasks, edges = _layered_fanout(n_waves=4, per_wave=5)
    assert len(tasks) == 20
    layout = build_plan_layout(tasks, edges=edges, free_slots=3)
    assert [w.index for w in layout.waves] == [0, 1, 2, 3]
    for w in layout.waves:
        assert [c.id for c in w.cards] == [f"W{w.index}_{i}" for i in range(5)]
    assert layout.critical_path[0] == "W0_0"
    assert layout.critical_path[-1] == "W3_0"
    assert len(layout.critical_path) == 4
    w0 = layout.waves[0]
    assert w0.parallel_count == 5 and w0.free_slots == 3 and w0.queued == 2


# ---------------------------------------------------------------------------
# Golden: synthetic 50-node chain-of-diamonds
# ---------------------------------------------------------------------------


def _chain_of_diamonds(n_diamonds: int):
    """Each diamond: in -> {l, r} -> out, out feeds the next diamond's in.
    4 nodes/diamond; 12 diamonds ~ 48 nodes + 2 extra singles = 50."""
    tasks = []
    edges = []
    prev_out = None
    for d in range(n_diamonds):
        in_id, l_id, r_id, out_id = (f"D{d}_in", f"D{d}_l", f"D{d}_r", f"D{d}_out")
        state = "ready" if prev_out is None else "open"
        tasks += [_n(in_id, state=state), _n(l_id, state="open"),
                  _n(r_id, state="open"), _n(out_id, state="open")]
        edges += [(l_id, in_id), (r_id, in_id), (out_id, l_id), (out_id, r_id)]
        if prev_out is not None:
            edges.append((in_id, prev_out))
        prev_out = out_id
    return tasks, edges


def test_golden_50_node_chain_of_diamonds():
    tasks, edges = _chain_of_diamonds(12)
    tasks += [_n("extra_ready", state="ready"), _n("extra_open", state="open")]
    edges.append(("extra_open", "extra_ready"))
    assert len(tasks) == 50
    layout = build_plan_layout(tasks, edges=edges, free_slots=4)
    # 12 diamonds x 3 waves/diamond = 36; the extra pair shares waves 0 and 1.
    assert len(layout.waves) == 36
    assert layout.waves[0].index == 0
    total_cards = sum(len(w.cards) for w in layout.waves)
    assert total_cards == 50
    # Critical path threads every diamond: in, l (tie-break over r), out - x12.
    expected_cp = []
    for d in range(12):
        expected_cp += [f"D{d}_in", f"D{d}_l", f"D{d}_out"]
    assert layout.critical_path == tuple(expected_cp)


# ---------------------------------------------------------------------------
# Fold summary — overflow pruning
# ---------------------------------------------------------------------------


def test_fold_summary_absent_when_under_budget():
    tasks = [_n(f"T{i}", state="ready" if i == 0 else "open") for i in range(5)]
    edges = [(f"T{i}", f"T{i - 1}") for i in range(1, 5)]
    layout = build_plan_layout(tasks, edges=edges, free_slots=1, max_visible_topics=10)
    assert layout.fold_summary is None
    assert len(layout.waves) == 5


def test_fold_summary_drops_trailing_waves_over_budget():
    tasks = [_n(f"T{i}", state="ready" if i == 0 else "open") for i in range(5)]
    edges = [(f"T{i}", f"T{i - 1}") for i in range(1, 5)]
    layout = build_plan_layout(tasks, edges=edges, free_slots=1, max_visible_topics=2)
    assert layout.fold_summary == "…3 more open nodes, z expands"
    assert [w.index for w in layout.waves] == [0, 1]
    # Edges into folded topics are dropped along with the cards.
    visible_ids = {c.id for w in layout.waves for c in w.cards}
    assert all(e.src in visible_ids and e.dst in visible_ids for e in layout.edges)


def test_fold_summary_absent_when_wave_zero_alone_exceeds_budget():
    """Wave 0 is never hidden — if it alone exceeds the budget it is shown in
    full and nothing was actually dropped, so there is no fold summary."""
    tasks = [_n(f"T{i}", state="ready") for i in range(5)]
    layout = build_plan_layout(tasks, edges=[], free_slots=1, max_visible_topics=2)
    assert len(layout.waves) == 1
    assert len(layout.waves[0].cards) == 5
    assert layout.fold_summary is None
