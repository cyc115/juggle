"""juggle_frontier_render tests, incl. narrow-viewport degradation (fr-smoke,
2026-07-02): agent info drops before titles truncate, matching the pinned
degradation order (hide agent column first, then truncate names)."""
from juggle_frontier_layout import FrontierRow
from juggle_frontier_render import render_node_row, WIDTH_HIDE_AGENT, WIDTH_TRUNCATE_TITLE


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
