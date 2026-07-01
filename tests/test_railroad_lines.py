"""Pure railroad line-model tests (2026-06-30 graph railroad, T4)."""
from juggle_cockpit_graph_lanes import assign_lanes
from juggle_cockpit_railroad_lines import railroad_lines
from juggle_cockpit_graph_layout import GraphTask


def _t(i, s="open"):
    return GraphTask(i, i.upper(), s)


def test_linear_rail_column():
    """2026-06-30 graph railroad: linear chain rail is a single │ column."""
    tasks = [_t("a", "verified"), _t("b", "running"), _t("c", "open")]
    L = assign_lanes(tasks, [("b", "a"), ("c", "b")])
    lines = railroad_lines(L, tasks, selected_row=1)
    assert [ln.id for ln in lines] == ["a", "b", "c"]
    assert lines[1].selected is True and lines[1].glyph == "◐"
    assert all(set(ln.rail) <= set("│ ") for ln in lines)  # no branch/merge


def test_diamond_has_branch_and_merge_rows():
    """2026-06-30 graph railroad: diamond rail shows a branch then a merge."""
    tasks = [_t("a"), _t("b"), _t("c"), _t("d")]
    L = assign_lanes(tasks, [("b", "a"), ("c", "a"), ("d", "b"), ("d", "c")])
    lines = railroad_lines(L, tasks, selected_row=0)
    joined = "\n".join(ln.rail for ln in lines)
    assert any(ch in joined for ch in "┬├") and any(ch in joined for ch in "┴┤")


def test_row_and_title_carry_through():
    """2026-06-30 graph railroad: RailLine surfaces row, id, title, glyph."""
    tasks = [_t("a", "verified"), _t("b", "open")]
    L = assign_lanes(tasks, [("b", "a")])
    lines = railroad_lines(L, tasks, selected_row=0)
    assert lines[0].row == 0 and lines[0].id == "a" and lines[0].title == "A"
    assert lines[0].glyph == "●" and lines[0].selected is True
    assert lines[1].selected is False
