"""Characterization pins for the extracted graph row renderer (R1, 2026-06-30 graph railroad).

The seven per-project row helpers moved VERBATIM out of juggle_cockpit_graph_panel
into juggle_cockpit_graph_rows; behavior must be byte-identical.
"""
from juggle_cockpit_graph_rows import (
    _RUNNING_STATES,
    _STATE_COLORS,
    _progress_bar,
    topological_order,
)
from juggle_cockpit_graph_layout import GraphTask


def test_progress_bar_fraction():
    tasks = [GraphTask("a", "A", "verified"), GraphTask("b", "B", "open")]
    bar = _progress_bar(tasks, width=10)
    assert bar.count("█") == 5 and bar.startswith("▕") and bar.endswith("▏")


def test_topological_order_stable():
    tasks = [GraphTask("b", "B", "open"), GraphTask("a", "A", "open")]
    edges = [("b", "a")]
    assert [n.id for n in topological_order(tasks, edges)] == ["a", "b"]


def test_progress_bar_does_not_count_unlanded_as_done():
    """integrated-unlanded counts for progress display but not as 'done'
    (SPEC §5.4) — only 'verified' fills the bar."""
    tasks = [GraphTask("a", "A", "integrated-unlanded"), GraphTask("b", "B", "open")]
    bar = _progress_bar(tasks, width=10)
    assert bar.count("█") == 0


def test_unlanded_has_a_state_color():
    assert _STATE_COLORS["integrated-unlanded"] == "blue"


def test_unlanded_counts_as_running():
    assert "integrated-unlanded" in _RUNNING_STATES
