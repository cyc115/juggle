"""Characterization pins for the extracted graph row renderer (R1, 2026-06-30 graph railroad).

The seven per-project row helpers moved VERBATIM out of juggle_cockpit_graph_panel
into juggle_cockpit_graph_rows; behavior must be byte-identical.
"""
from juggle_cockpit_graph_rows import _progress_bar, topological_order
from juggle_cockpit_graph_layout import GraphTask


def test_progress_bar_fraction():
    tasks = [GraphTask("a", "A", "verified"), GraphTask("b", "B", "open")]
    bar = _progress_bar(tasks, width=10)
    assert bar.count("█") == 5 and bar.startswith("▕") and bar.endswith("▏")


def test_topological_order_stable():
    tasks = [GraphTask("b", "B", "open"), GraphTask("a", "A", "open")]
    edges = [("b", "a")]
    assert [n.id for n in topological_order(tasks, edges)] == ["a", "b"]
