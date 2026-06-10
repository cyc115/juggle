"""TDD tests for the graph panel Rich renderable builder.

build_graph_panel turns (GraphDag-ish input + selection + unread badge + width)
into a Rich Panel: header progress line, per-rank columns flowing rightward,
state glyphs, selection highlight, unread badge in the title, minimap when
panned, and the "no armed graph" path. Pure (Rich only).
"""
from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cockpit_graph_layout import GraphNode  # noqa: E402
from juggle_cockpit_graph_panel import build_graph_panel  # noqa: E402
from juggle_cockpit_view import NODE_STATE_GLYPHS  # noqa: E402


def _text(panel, width=80) -> str:
    from rich.console import Console

    buf = io.StringIO()
    Console(width=width, file=buf, no_color=True, highlight=False).print(panel)
    return buf.getvalue()


def _dag():
    nodes = [
        GraphNode("a", "Setup", "verified"),
        GraphNode("b", "Build", "running", thread_id="t1"),
        GraphNode("c", "Ship", "ready"),
    ]
    edges = [("b", "a"), ("c", "b")]
    return nodes, edges


# ---------------------------------------------------------------------------
# Header + progress
# ---------------------------------------------------------------------------


def test_header_shows_project_and_progress():
    nodes, edges = _dag()
    panel = build_graph_panel(
        project_id="oauth-login", nodes=nodes, edges=edges,
        selection=0, unread=0, width=80, height=20, pan_offset=0,
    )
    out = _text(panel)
    assert "oauth-login" in out
    assert "done" in out  # format_progress segment "1/3 done"


# ---------------------------------------------------------------------------
# Node glyphs from state
# ---------------------------------------------------------------------------


def test_node_glyphs_from_state():
    nodes, edges = _dag()
    panel = build_graph_panel(
        project_id="p", nodes=nodes, edges=edges,
        selection=0, unread=0, width=80, height=20, pan_offset=0,
    )
    out = _text(panel)
    assert NODE_STATE_GLYPHS["verified"] in out
    assert NODE_STATE_GLYPHS["running"] in out
    assert NODE_STATE_GLYPHS["ready"] in out


# ---------------------------------------------------------------------------
# Unread badge
# ---------------------------------------------------------------------------


def test_unread_badge_in_title():
    nodes, edges = _dag()
    panel = build_graph_panel(
        project_id="p", nodes=nodes, edges=edges,
        selection=0, unread=3, width=80, height=20, pan_offset=0,
    )
    assert "3" in str(panel.title)
    assert "Graph" in str(panel.title)


def test_no_badge_when_zero_unread():
    nodes, edges = _dag()
    panel = build_graph_panel(
        project_id="p", nodes=nodes, edges=edges,
        selection=0, unread=0, width=80, height=20, pan_offset=0,
    )
    title = str(panel.title)
    assert "Graph" in title
    # no "· N" badge segment
    assert "·" not in title or "0" not in title


# ---------------------------------------------------------------------------
# No armed graph
# ---------------------------------------------------------------------------


def test_no_armed_graph_hint():
    panel = build_graph_panel(
        project_id=None, nodes=[], edges=[],
        selection=0, unread=0, width=80, height=20, pan_offset=0,
    )
    out = _text(panel)
    assert "no armed" in out.lower()


def test_armed_but_empty_graph_hint():
    panel = build_graph_panel(
        project_id="p", nodes=[], edges=[],
        selection=0, unread=0, width=80, height=20, pan_offset=0,
    )
    out = _text(panel)
    assert "no" in out.lower() and ("node" in out.lower() or "graph" in out.lower())


# ---------------------------------------------------------------------------
# Narrow width never overflows
# ---------------------------------------------------------------------------


def test_narrow_width_no_overflow():
    nodes = [GraphNode(f"n{i}", f"Task {i}", "verified" if i < 8 else "ready") for i in range(10)]
    edges = [(f"n{i}", f"n{i-1}") for i in range(1, 10)]
    panel = build_graph_panel(
        project_id="p", nodes=nodes, edges=edges,
        selection=0, unread=0, width=40, height=20, pan_offset=0,
    )
    out = _text(panel, width=40)
    for line in out.splitlines():
        assert len(line) <= 40, f"overflow: {len(line)} > 40: {line!r}"
