"""Regression pins for the 2026-06-30 graph railroad (Options D + B).

These pins are the refactor safety net for the pure railroad core: lane
determinism, the spine width invariant, single-cell glyphs, and the cycle
guard. Each names the incident so it may never be silently weakened.
"""
import unicodedata

from juggle_cockpit_graph_lanes import assign_lanes
from juggle_cockpit_graph_layout import GraphTask
from juggle_cockpit_graph_spine import spine_plain
from juggle_cockpit_legend import RAILROAD_STATE_GLYPHS


def _t(i, s="open"):
    return GraphTask(i, i.upper(), s)


def test_lane_assignment_is_byte_stable():
    """2026-06-30 graph railroad: diamond lanes are deterministic across 100 runs.

    Guards against set-ordering flakiness leaking into lane packing.
    """
    tasks = [_t("a"), _t("b"), _t("c"), _t("d")]
    edges = [("b", "a"), ("c", "a"), ("d", "b"), ("d", "c")]
    first = [(n.id, n.row, n.lane) for n in assign_lanes(tasks, edges).nodes]
    for _ in range(100):
        again = [(n.id, n.row, n.lane) for n in assign_lanes(tasks, edges).nodes]
        assert again == first


def test_spine_never_exceeds_width():
    """2026-06-30 graph railroad: spine_plain never overflows the pane width.

    A 50-node wide fan at the narrowest-to-widest viewports must stay bounded.
    """
    tasks = [_t("r", "verified")] + [_t(f"m{i}", "running") for i in range(50)]
    edges = [(f"m{i}", "r") for i in range(50)]
    layout = assign_lanes(tasks, edges)
    for width in (80, 120, 240):
        assert len(spine_plain(layout, width=width)) <= width


def test_railroad_glyphs_are_single_cell():
    """2026-06-30 graph railroad: every spine glyph is single-width (no emoji).

    Guards against a future double-width glyph creeping in and breaking the
    width-based compression / truncation.
    """
    for g in RAILROAD_STATE_GLYPHS.values():
        assert len(g) == 1
        assert unicodedata.east_asian_width(g) not in ("W", "F")


def test_two_cycle_terminates_with_finite_lanes():
    """2026-06-30 graph railroad: a 2-cycle terminates with finite lanes.

    Reuses build_ranks' pass cap as the cycle guard — must not hang.
    """
    tasks = [_t("a"), _t("b")]
    edges = [("a", "b"), ("b", "a")]
    layout = assign_lanes(tasks, edges)
    assert len(layout.nodes) == 2
    assert layout.lane_count >= 1
    assert all(isinstance(n.lane, int) and n.lane >= 0 for n in layout.nodes)
