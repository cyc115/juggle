"""Pure lane-assignment core truth table (T1, 2026-06-30 graph railroad)."""
from juggle_cockpit_graph_lanes import assign_lanes
from juggle_cockpit_graph_layout import GraphTask


def _t(i, s="open"):
    return GraphTask(i, i.upper(), s)


def _lane(layout):
    return {n.id: n.lane for n in layout.nodes}


def test_linear_chain_single_lane():
    """2026-06-30 graph railroad: c→b→a packs into one lane."""
    tasks = [_t("a"), _t("b"), _t("c")]
    edges = [("b", "a"), ("c", "b")]
    L = assign_lanes(tasks, edges)
    assert _lane(L) == {"a": 0, "b": 0, "c": 0} and L.lane_count == 1


def test_diamond():
    """2026-06-30 graph railroad: diamond fan-out then merge."""
    tasks = [_t("a"), _t("b"), _t("c"), _t("d")]
    edges = [("b", "a"), ("c", "a"), ("d", "b"), ("d", "c")]
    L = assign_lanes(tasks, edges)
    lanes = _lane(L)
    assert lanes == {"a": 0, "b": 0, "c": 1, "d": 0} and L.lane_count == 2
    by = {n.id: n for n in L.nodes}
    assert by["a"].fan_out == 2 and by["d"].fan_in == 2


def test_wide_fanout():
    """2026-06-30 graph railroad: root → M1..M5 uses 5 lanes."""
    tasks = [_t("r")] + [_t(f"m{i}") for i in range(1, 6)]
    edges = [(f"m{i}", "r") for i in range(1, 6)]
    L = assign_lanes(tasks, edges)
    lanes = _lane(L)
    assert lanes["r"] == 0 and sorted(lanes[f"m{i}"] for i in range(1, 6)) == [0, 1, 2, 3, 4]
    assert L.lane_count == 5


def test_multiple_roots():
    """2026-06-30 graph railroad: two independent chains get distinct lanes."""
    tasks = [_t("a0"), _t("a1"), _t("b0"), _t("b1")]
    edges = [("a1", "a0"), ("b1", "b0")]  # two 2-chains
    L = assign_lanes(tasks, edges)
    lanes = _lane(L)
    assert lanes["a0"] != lanes["b0"] or lanes["a1"] != lanes["b1"]
    assert L.lane_count >= 2
