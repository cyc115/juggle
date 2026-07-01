"""Pure tree-render helper (2026-06-30 topic-graph-state-unify R3)."""
from juggle_cockpit_tree import TreeChild, tree_lines

_G = lambda s: {"verified": "●", "running": "◐"}.get(s, "○")  # noqa: E731


def test_expanded_lists_each_child():
    kids = [TreeChild("t1", "verified"), TreeChild("t2", "running")]
    lines = tree_lines("[K] build login", kids, expanded=True, glyph_for=_G, width=40)
    plain = [line.plain for line in lines]
    assert plain[0] == "[K] build login"
    assert any("t1" in p and "●" in p for p in plain[1:])
    assert any("t2" in p and "◐" in p for p in plain[1:])
    assert len(plain) == 3


def test_collapsed_rolls_up():
    kids = [TreeChild("t1", "verified"), TreeChild("t2", "verified")]
    lines = tree_lines("[K] build login", kids, expanded=False, glyph_for=_G, width=40)
    plain = [line.plain for line in lines]
    assert len(plain) == 2
    assert "2/2 done" in plain[1]


def test_no_children_parent_only():
    lines = tree_lines("[K] idea", [], expanded=True, glyph_for=_G, width=40)
    assert [line.plain for line in lines] == ["[K] idea"]
