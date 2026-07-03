"""juggle_frontier_render tests, incl. narrow-viewport degradation (fr-smoke,
2026-07-02): agent info drops before titles truncate, matching the pinned
degradation order (hide agent column first, then truncate names)."""
from juggle_cockpit_graph_layout import GraphTask
from juggle_frontier_layout import FrontierRow, build_frontier_layout
from juggle_frontier_rails import RailCell
from juggle_frontier_render import (
    render_node_row, render_wave_band, strip_markup,
    WIDTH_HIDE_AGENT, WIDTH_TRUNCATE_TITLE,
)


def _running_row(title="A Running Topic With A Long Name"):
    return FrontierRow(id="a", title=title, state="running", kind="running", wave=0, lane=0)


def test_wide_viewport_shows_elapsed_and_agent():
    row = _running_row()
    line = render_node_row(row, 1, selected=False, meta={"elapsed": "12m", "agent": "coder·sonnet"}, width=200)
    assert "12m" in line and "coder·sonnet" in line
    assert row.title in line


def test_narrow_viewport_hides_agent_before_title():
    row = _running_row()
    line = render_node_row(
        row, 1, selected=False, meta={"elapsed": "12m", "agent": "coder·sonnet"},
        width=WIDTH_HIDE_AGENT - 1,
    )
    assert "12m" in line
    assert "coder·sonnet" not in line
    assert row.title in line  # title not yet truncated at this width


def test_very_narrow_viewport_also_truncates_title():
    row = _running_row()
    line = render_node_row(
        row, 1, selected=False, meta={"elapsed": "12m", "agent": "coder·sonnet"},
        width=WIDTH_TRUNCATE_TITLE - 1,
    )
    assert "coder·sonnet" not in line
    assert row.title not in line
    assert "…" in line


def test_no_width_means_no_degradation():
    row = _running_row()
    line = render_node_row(row, 1, selected=False, meta={"elapsed": "12m", "agent": "coder·sonnet"})
    assert "coder·sonnet" in line and row.title in line


def test_wave_band_rule_width_uses_plain_gutter_width_not_markup_length():
    """Regression pin (2026-07-03): the dotted rule's fill length was computed
    from the MARKUP-length of the gutter string (colored cells add
    "[color]...[/]" bytes), so two bands with the same lane_count but a
    different number of colored pass-through rails rendered visibly different
    total widths in the same terminal column budget. Fill must be computed
    from the gutter's PLAIN (cell-count) width."""
    band = FrontierRow(id="__wave_1__", title="", state="", kind="wave-band",
                        wave=1, parallel_count=2, free_slots=1, queued=1)
    no_color = [RailCell(" ") for _ in range(3)]
    all_colored = [RailCell("│", "dark_orange") for _ in range(3)]

    line_no_color = strip_markup(render_wave_band(band, 3, gutter_cells=no_color, width=76))
    line_colored = strip_markup(render_wave_band(band, 3, gutter_cells=all_colored, width=76))
    assert len(line_no_color) == len(line_colored)


def test_wave_band_pass_through_rail_visible_in_gutter():
    tasks = [GraphTask("a", "A", "ready")] + [GraphTask(f"w{i}", f"W{i}", "open") for i in range(3)] + [GraphTask("z", "Z", "open")]
    edges = [(f"w{i}", "a") for i in range(3)] + [("z", "w0")]
    layout = build_frontier_layout(tasks, edges, free_slots=4)
    from juggle_frontier_rails import build_rail_gutters

    grid = build_rail_gutters(layout, edges)
    band_i = next(i for i, r in enumerate(layout.rows) if r.kind == "wave-band")
    line = render_wave_band(layout.rows[band_i], layout.lane_count, gutter_cells=grid[band_i], width=76)
    assert "│" in line or "║" in line
