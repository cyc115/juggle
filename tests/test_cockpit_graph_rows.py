"""Characterization pins for the extracted graph row renderer (R1, 2026-06-30 graph railroad).

The seven per-project row helpers moved VERBATIM out of juggle_cockpit_graph_panel
into juggle_cockpit_graph_rows; behavior must be byte-identical.
"""
from juggle_cockpit_graph_rows import (
    _RUNNING_STATES,
    _STATE_COLORS,
    _cell_text,
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
    # Richer-state palette (2026-07-03): unlanded gets its own purple, distinct
    # from integrating's blue (was a shared "blue" before the rewrite).
    assert _STATE_COLORS["integrated-unlanded"] == "medium_purple"


def test_unlanded_counts_as_running():
    assert "integrated-unlanded" in _RUNNING_STATES


# ── 2026-07-03 railroad richer-state palette ──────────────────────────────────

_ALL_STATES = (
    "verified", "ready", "dispatching", "running", "integrating",
    "integrated-unlanded", "open", "cancelled", "blocked-failed",
    "failed-verify", "failed-exec", "failed-integration",
)


def test_twelve_states_map_to_distinct_colors():
    """Richer-state palette (spec 2026-07-03 §1): every one of the 12 lifecycle
    states resolves to its OWN colour — no two share (the old palette collapsed
    3 states onto yellow and 4 onto red)."""
    colours = [_STATE_COLORS[s] for s in _ALL_STATES]   # KeyError => missing state
    assert len(set(colours)) == len(colours), (
        f"states share colours: {colours}"
    )


def test_cancelled_is_dim_grey():
    """spec §1: cancelled → grey37 rendered dim (pruned/inert)."""
    assert _STATE_COLORS["cancelled"] == "grey37"
    cell = _cell_text(
        1, GraphTask("x", "X", "cancelled"), idx_w=1,
        dep_num=None, cell_w=30, selected=False,
    )
    assert cell.style.dim is True
    assert cell.style.color.name == "grey37"
