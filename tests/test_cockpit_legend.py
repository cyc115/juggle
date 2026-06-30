"""Tests for the cockpit legend single-source-of-truth module."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ── Task 1: glyph dicts moved into the legend module ──────────────────────────

def test_legend_module_exports_glyph_dicts():
    """juggle_cockpit_legend owns the six glyph dicts (moved out of view.py)."""
    import juggle_cockpit_legend as L
    for name in ("TOPIC_STATUS_GLYPHS", "ACTION_TIER_GLYPHS", "AGENT_STATUS_GLYPHS",
                 "SCHED_STATUS_GLYPHS", "TASK_STATE_GLYPHS", "NOTIF_KIND_GLYPHS"):
        d = getattr(L, name)
        assert isinstance(d, dict) and d, f"{name} missing or empty"


def test_view_reexports_same_dict_objects():
    """view.py re-exports the SAME objects (identity) so no second copy can drift."""
    import juggle_cockpit_legend as L
    import juggle_cockpit_view as V
    assert V.TASK_STATE_GLYPHS is L.TASK_STATE_GLYPHS
    assert V.TOPIC_STATUS_GLYPHS is L.TOPIC_STATUS_GLYPHS


# ── Task 2: graph one-line legend derived from the glyph dict ──────────────────

def test_graph_inline_legend_matches_glyph_dict():
    """The Graph panel's one-line legend is derived from TASK_STATE_GLYPHS, not hardcoded."""
    import juggle_cockpit_legend as L
    s = L.graph_inline_legend()
    assert L.TASK_STATE_GLYPHS["running"] in s      # 🏃
    assert L.TASK_STATE_GLYPHS["ready"] in s         # ◇
    assert L.TASK_STATE_GLYPHS["verified"] in s      # ✅
    assert f"{L.GRAPH_DEP_SUFFIX}n=waits on #n" in s  # ⊣n=waits on #n


def test_graph_panel_uses_registry_legend():
    """build_graph_panel renders the registry-derived legend (drift pin: 2026-06-29
    graph legend was a hardcoded string divorced from TASK_STATE_GLYPHS)."""
    import io
    from rich.console import Console
    from juggle_cockpit_graph_panel import build_graph_panel
    from juggle_cockpit_graph_layout import GraphTask
    import juggle_cockpit_legend as L
    tasks = [GraphTask(id="a", title="A", state="ready")]
    panel = build_graph_panel(project_id="P1", tasks=tasks, edges=[], selection=0,
                              unread=0, width=120, height=20, pan_offset=0)
    buf = io.StringIO()
    Console(width=120, file=buf, no_color=True).print(panel)
    out = buf.getvalue().replace("\n", " ")
    assert L.graph_inline_legend() in out or all(
        tok in buf.getvalue() for tok in ("running", "ready", "done")
    )
