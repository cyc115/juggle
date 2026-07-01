"""Surface-D dependency spine (T3, 2026-06-30 graph railroad)."""
from juggle_cockpit_graph_lanes import assign_lanes
from juggle_cockpit_graph_spine import spine_plain
from juggle_cockpit_graph_layout import GraphTask


def _t(i, s):
    return GraphTask(i, i.upper(), s)


def test_linear_spine_plain():
    """2026-06-30 graph railroad: linear chain renders dots joined by ─."""
    L = assign_lanes([_t("a", "verified"), _t("b", "running"), _t("c", "open")],
                     [("b", "a"), ("c", "b")])
    s = spine_plain(L, width=40)
    assert s.startswith("●─◐─·")
    assert s.rstrip().endswith("1/3")   # 1 verified of 3


def test_diamond_has_fan_markers():
    """2026-06-30 graph railroad: fan-out shows ┬, fan-in shows ┴."""
    L = assign_lanes([_t("a", "verified"), _t("b", "running"), _t("c", "ready"), _t("d", "open")],
                     [("b", "a"), ("c", "a"), ("d", "b"), ("d", "c")])
    s = spine_plain(L, width=40)
    assert "┬" in s and "┴" in s


def test_never_exceeds_width():
    """2026-06-30 graph railroad: spine never overflows the pane width."""
    tasks = [_t("r", "verified")] + [_t(f"m{i}", "running") for i in range(20)]
    edges = [(f"m{i}", "r") for i in range(20)]
    L = assign_lanes(tasks, edges)
    s = spine_plain(L, width=30, lane_cap=6)
    assert len(s) <= 30
    assert "⧉" in s or "…" in s   # compressed
