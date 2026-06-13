"""TDD tests for the pure task-graph layout engine.

The layout engine (juggle_cockpit_graph_layout) turns (tasks, edges) into a
left-to-right layered DAG: rank assignment via longest dependency depth,
ordered cells per rank, narrow-width collapse (focus+context), and horizontal
pan windowing with a minimap. Pure — no Textual, no DB.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cockpit_graph_layout import (  # noqa: E402
    GraphTask,
    assign_ranks,
    build_ranks,
    collapse_ranks,
    minimap_bar,
    pan_window,
)


def _n(task_id, state="pending", title="", thread_id=None):
    return GraphTask(id=task_id, title=title or task_id, state=state, thread_id=thread_id)


# ---------------------------------------------------------------------------
# Rank assignment — longest dependency depth
# ---------------------------------------------------------------------------


def test_assign_ranks_diamond():
    """A→B, A→C, B→D, C→D: a diamond. D's rank = longest path = 2, not 1."""
    tasks = [_n("A"), _n("B"), _n("C"), _n("D")]
    edges = [("B", "A"), ("C", "A"), ("D", "B"), ("D", "C")]
    ranks = assign_ranks(tasks, edges)
    assert ranks["A"] == 0
    assert ranks["B"] == 1
    assert ranks["C"] == 1
    assert ranks["D"] == 2


def test_assign_ranks_longest_path_not_shortest():
    """A→B→C and A→C: C must rank 2 (longest path), proving honest diamonds."""
    tasks = [_n("A"), _n("B"), _n("C")]
    edges = [("B", "A"), ("C", "B"), ("C", "A")]
    ranks = assign_ranks(tasks, edges)
    assert ranks["C"] == 2


def test_assign_ranks_single_task_no_edges():
    tasks = [_n("solo")]
    ranks = assign_ranks(tasks, [])
    assert ranks == {"solo": 0}


def test_assign_ranks_disconnected():
    """Two disconnected roots both rank 0."""
    tasks = [_n("A"), _n("B")]
    ranks = assign_ranks(tasks, [])
    assert ranks["A"] == 0 and ranks["B"] == 0


def test_assign_ranks_cycle_does_not_hang():
    """Defensive: a (malformed) cycle must terminate, not loop forever."""
    tasks = [_n("A"), _n("B")]
    edges = [("A", "B"), ("B", "A")]
    ranks = assign_ranks(tasks, edges)  # must return, finite ranks
    assert set(ranks) == {"A", "B"}


# ---------------------------------------------------------------------------
# build_ranks — ordered cells per rank
# ---------------------------------------------------------------------------


def test_build_ranks_groups_by_rank_stable_order():
    tasks = [_n("C"), _n("A"), _n("B"), _n("D")]
    edges = [("B", "A"), ("C", "A"), ("D", "B"), ("D", "C")]
    ranks = build_ranks(tasks, edges)
    # rank 0 = [A], rank 1 = [B, C] (id order), rank 2 = [D]
    assert [c.id for c in ranks[0].tasks] == ["A"]
    assert [c.id for c in ranks[1].tasks] == ["B", "C"]
    assert [c.id for c in ranks[2].tasks] == ["D"]


# ---------------------------------------------------------------------------
# collapse_ranks — focus+context narrow folding
# ---------------------------------------------------------------------------


def test_collapse_folds_verified_and_far_pending_keeps_active():
    """Narrow budget: verified + pending ranks fold to counts; ready/running/
    failed ranks stay expanded (focus+context)."""
    # 6 ranks: r0 verified, r1 verified, r2 ready, r3 running, r4 failed, r5 pending
    tasks = [
        _n("a", "verified"), _n("b", "verified"), _n("c", "ready"),
        _n("d", "running"), _n("e", "failed-verify"), _n("f", "pending"),
    ]
    edges = [("b", "a"), ("c", "b"), ("d", "c"), ("e", "d"), ("f", "e")]
    ranks = build_ranks(tasks, edges)
    collapsed = collapse_ranks(ranks, max_visible_ranks=4)
    kinds = [("collapsed" if r.collapsed else "expanded") for r in collapsed]
    # ready/running/failed ranks (2,3,4) must remain expanded
    expanded_states = {
        r.tasks[0].state for r in collapsed if not r.collapsed and r.tasks
    }
    assert "ready" in expanded_states
    assert "running" in expanded_states
    assert any("failed" in s for s in expanded_states)
    # at least one fold happened to fit the budget
    assert "collapsed" in kinds


def test_collapse_noop_when_fits():
    tasks = [_n("a", "ready"), _n("b", "running")]
    edges = [("b", "a")]
    ranks = build_ranks(tasks, edges)
    collapsed = collapse_ranks(ranks, max_visible_ranks=10)
    assert all(not r.collapsed for r in collapsed)


def test_collapse_count_label():
    tasks = [_n("a", "verified"), _n("b", "verified"), _n("c", "ready")]
    edges = [("b", "a"), ("c", "b")]
    ranks = build_ranks(tasks, edges)
    collapsed = collapse_ranks(ranks, max_visible_ranks=1)
    folded = [r for r in collapsed if r.collapsed]
    assert folded, "expected a folded rank"
    assert any("verified" in r.label for r in folded)


# ---------------------------------------------------------------------------
# pan_window + minimap
# ---------------------------------------------------------------------------


def test_pan_window_slices_visible_ranks():
    ranks = build_ranks([_n(x) for x in "ABCDEF"], [])  # 1 rank of 6 actually
    # force 6 ranks via a chain
    tasks = [_n(x) for x in "ABCDEF"]
    edges = [("B", "A"), ("C", "B"), ("D", "C"), ("E", "D"), ("F", "E")]
    ranks = build_ranks(tasks, edges)
    visible, mm = pan_window(ranks, offset=2, visible_count=3)
    assert [r.index for r in visible] == [2, 3, 4]
    assert mm.first == 2 and mm.last == 4 and mm.total == 6


def test_pan_window_clamps_offset():
    tasks = [_n(x) for x in "ABCD"]
    edges = [("B", "A"), ("C", "B"), ("D", "C")]
    ranks = build_ranks(tasks, edges)
    visible, mm = pan_window(ranks, offset=99, visible_count=2)
    # clamped so the last ranks show
    assert [r.index for r in visible] == [2, 3]


def test_minimap_bar_marks_visible_segment():
    bar = minimap_bar(total=6, first=2, last=4)
    assert "ranks 3–5/6" in bar or "ranks 2–4/6" in bar  # 0- or 1-based label
    # filled glyphs for visible, faint for hidden
    assert any(ch in bar for ch in ("█", "▇"))


# ---------------------------------------------------------------------------
# width budget — wide graph never exceeds a column count
# ---------------------------------------------------------------------------


def test_wide_graph_collapses_within_budget():
    """A 30-task chain (30 ranks) collapses to <= the visible-rank budget."""
    tasks = [_n(f"n{i}", "verified" if i < 25 else "ready") for i in range(30)]
    edges = [(f"n{i}", f"n{i-1}") for i in range(1, 30)]
    ranks = build_ranks(tasks, edges)
    assert len(ranks) == 30
    collapsed = collapse_ranks(ranks, max_visible_ranks=6)
    assert len(collapsed) <= 6
