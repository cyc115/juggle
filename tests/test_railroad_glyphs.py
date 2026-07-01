"""Single-cell railroad glyphs (T2, 2026-06-30 graph railroad)."""
import unicodedata

from juggle_cockpit_legend import RAILROAD_STATE_GLYPHS, railroad_glyph


def test_single_cell_glyphs():
    """2026-06-30 graph railroad: spine glyphs are single-width (no emoji)."""
    for g in RAILROAD_STATE_GLYPHS.values():
        assert len(g) == 1 and unicodedata.east_asian_width(g) not in ("W", "F")


def test_state_mapping():
    assert railroad_glyph("verified") == "●"
    assert railroad_glyph("running") == "◐"
    assert railroad_glyph("ready") == "○"
    assert railroad_glyph("blocked-failed") == "◇"
    assert railroad_glyph("failed-exec") == "✗"
    assert railroad_glyph("open") == "·"
    assert railroad_glyph("weird-unknown") == "·"
