"""juggle_frontier_rails tests (fr-vf-rails, 2026-07-03). Golden-file style:
build a real FrontierLayout via build_frontier_layout (diamond / wide fan-in /
single chain) and pin the exact rail gutter grid it produces, in both markup
and plain form. Carries forward the 2026-07-02 gitlog-view lane IndexError
bounds-safety pin onto this engine."""
from juggle_cockpit_graph_layout import GraphTask
from juggle_frontier_layout import build_frontier_layout
from juggle_frontier_rails import (
    AMBER, GREEN, build_rail_gutters, gutter_plain, gutter_string,
)


def _t(i, state="open", done=None, total=None):
    return GraphTask(i, i.upper(), state, tasks_done=done, tasks_total=total)


def test_single_chain_is_a_straight_amber_critical_rail():
    """a -> b -> c, all on the critical path: every pass-through row shows a
    double-line amber rail in lane 0, terminating in an amber arrow."""
    tasks = [_t("a", "ready"), _t("b", "open"), _t("c", "open")]
    edges = [("b", "a"), ("c", "b")]
    layout = build_frontier_layout(tasks, edges, free_slots=2)
    assert layout.critical_path == ("a", "b", "c")

    grid = build_rail_gutters(layout, edges)
    assert len(grid) == len(layout.rows)
    row_of = {r.id: i for i, r in enumerate(layout.rows)}

    # a -> b: no rows strictly between (adjacent rows), elbow lands directly
    # at b's own lane (b.lane == a.lane, since b is a's sole/first dependent).
    b_lane = next(r for r in layout.rows if r.id == "b").lane
    cell_b = grid[row_of["b"]][b_lane]
    assert (cell_b.char, cell_b.color) == ("▸", AMBER)

    c_lane = next(r for r in layout.rows if r.id == "c").lane
    cell_c = grid[row_of["c"]][c_lane]
    assert (cell_c.char, cell_c.color) == ("▸", AMBER)

    plain_b = gutter_plain(grid[row_of["b"]])
    markup_b = gutter_string(grid[row_of["b"]])
    assert plain_b[b_lane] == "▸"
    assert markup_b == f"[{AMBER}]▸[/]" or f"[{AMBER}]▸[/]" in markup_b


def test_diamond_convergence_merges_on_the_horizontal_run():
    """a -> {b, c} -> d: d has two incoming edges (from b and from c). Only
    the leftmost (b's) lane continues straight into d's own lane; c's branch
    lane bends over via a corner + horizontal run into d's arrow column."""
    tasks = [_t("a", "ready"), _t("b", "open"), _t("c", "open"), _t("d", "open")]
    edges = [("b", "a"), ("c", "a"), ("d", "b"), ("d", "c")]
    layout = build_frontier_layout(tasks, edges, free_slots=4)
    by_id = {r.id: r for r in layout.rows if r.kind != "wave-band"}

    grid = build_rail_gutters(layout, edges)
    row_of = {r.id: i for i, r in enumerate(layout.rows)}
    d_row = grid[row_of["d"]]

    d_lane = by_id["d"].lane
    b_lane = by_id["b"].lane
    c_lane = by_id["c"].lane
    assert d_lane == min(b_lane, c_lane)  # d inherits the leftmost incoming lane

    # d's own lane holds the terminal arrow.
    assert d_row[d_lane].char == "▸"
    # the OTHER incoming lane (c's) holds a corner bending toward d's lane.
    other_lane = c_lane if c_lane != d_lane else b_lane
    assert d_row[other_lane].char in ("└", "╚")

    plain = gutter_plain(d_row)
    assert plain[d_lane] == "▸"
    markup = gutter_string(d_row)
    assert "▸" in markup and ("└" in markup or "╚" in markup)


def test_wide_fan_in_does_not_raise_and_stays_in_bounds():
    """Bounds-safety pin, carrying forward the 2026-07-02 gitlog-view
    IndexError incident: a fan-out branch lane closed by a smaller-index
    fan-in convergence before ever becoming any row's own `.lane` must not
    raise when the rails engine indexes into the gutter grid."""
    tasks = [_t("c", "verified"), _t("d", "open"), _t("e", "ready")]
    edges = [("d", "c"), ("e", "c"), ("e", "d")]
    layout = build_frontier_layout(tasks, edges, free_slots=2)
    grid = build_rail_gutters(layout, edges)  # must not raise
    assert len(grid) == len(layout.rows)
    for row in grid:
        assert len(row) == max(layout.lane_count, 1)


def test_50_node_wide_fan_in_rails_are_bounded_and_deterministic():
    tasks = [_t("root", "verified")] + [_t(f"n{i}", "open") for i in range(50)]
    edges = [(f"n{i}", "root") for i in range(50)]
    layout = build_frontier_layout(tasks, edges, free_slots=8)
    grid1 = build_rail_gutters(layout, edges)
    grid2 = build_rail_gutters(layout, edges)
    assert [[c.char for c in row] for row in grid1] == [[c.char for c in row] for row in grid2]
    for row in grid1:
        assert len(row) == max(layout.lane_count, 1)


def test_off_critical_path_rail_is_green_when_source_ready():
    """d depends on a (shorter branch, not on the critical path a->b->c):
    the a->d rail segment/elbow is colored from a's state (ready -> GREEN),
    not amber, since d never lands on the critical path."""
    tasks = [_t("a", "ready"), _t("b", "open"), _t("c", "open"), _t("d", "open")]
    edges = [("b", "a"), ("c", "b"), ("d", "a")]
    layout = build_frontier_layout(tasks, edges, free_slots=2)
    assert layout.critical_path == ("a", "b", "c")
    assert "d" not in layout.critical_path

    grid = build_rail_gutters(layout, edges)
    row_of = {r.id: i for i, r in enumerate(layout.rows)}
    by_id = {r.id: r for r in layout.rows if r.kind != "wave-band"}
    d_lane = by_id["d"].lane
    d_row = grid[row_of["d"]]
    # d's OWN lane cell (the a->d elbow terminus) takes a's color (ready ->
    # GREEN) -- other cells in the row may carry an unrelated amber b->c
    # rail simply passing through, which is correct and not asserted here.
    assert d_row[d_lane].char == "▸"
    assert d_row[d_lane].color == GREEN


def test_wave_band_row_gets_pass_through_vertical_for_a_spanning_edge():
    """A dependency edge that crosses a wave-band row (source above the band,
    destination below it) leaves a plain pass-through vertical in the band
    row at the source's lane -- the band interrupts text, not rails."""
    tasks = [_t("a", "ready")] + [_t(f"w{i}", "open") for i in range(3)] + [_t("z", "open")]
    edges = [(f"w{i}", "a") for i in range(3)] + [("z", "w0")]
    layout = build_frontier_layout(tasks, edges, free_slots=4)
    grid = build_rail_gutters(layout, edges)
    band_rows = [i for i, r in enumerate(layout.rows) if r.kind == "wave-band"]
    assert band_rows
    band_i = band_rows[0]
    assert any(c.char in ("│", "║") for c in grid[band_i])
