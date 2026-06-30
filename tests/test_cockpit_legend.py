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
