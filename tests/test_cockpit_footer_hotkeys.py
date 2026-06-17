"""Regression pin: cockpit footer must show ALL usable hotkeys.

Incident: g (toggle_graph), t (tail_toggle), f (focus_pane), / (filter),
and tab (cycle_pane) were bound with show=False and therefore absent from
the Textual Footer widget visible to the user.

This test inspects BINDINGS directly (no live TUI required) and pins
the complete set of keys that must appear in the footer.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# Keys that MUST appear in the footer (show=True, non-empty description).
# Navigation aliases (j/k/up/down/pgup/pgdn) and destructive ops (d) are
# intentionally omitted from the footer and are NOT in this set.
REQUIRED_SHOWN_KEYS = {
    "question_mark",  # ? Help
    "s",              # Switch
    "a",              # Ack
    "shift+c",        # Close
    "x",              # Archive
    "g",              # Toggle graph  ← was missing (show=False)
    "t",              # Tail toggle   ← was missing (show=False)
    "f",              # Focus pane    ← was missing (show=False)
    "slash",          # Filter        ← was missing (show=False)
    "tab",            # Cycle pane    ← was missing (show=False)
    "p",              # Projects modal ← new (Feature A)
    "w",              # Watchdog toggle  ← new (UX: footer visibility)
    "r",              # Watchdog restart ← new (UX: footer visibility)
}


def _shown_keys():
    """Return the set of keys in CockpitApp.BINDINGS that have show=True."""
    from juggle_cockpit import CockpitApp
    return {b.key for b in CockpitApp.BINDINGS if b.show and b.description}


def test_all_usable_hotkeys_shown_in_footer():
    """Every key in REQUIRED_SHOWN_KEYS must be in BINDINGS with show=True.

    Regression pin: 2026-06-13 — g/t/f/slash/tab had show=False so they
    were invisible in the Textual Footer widget.
    """
    shown = _shown_keys()
    missing = REQUIRED_SHOWN_KEYS - shown
    assert not missing, (
        f"These hotkeys are missing from the footer (show=False or absent): {sorted(missing)}\n"
        f"Currently shown: {sorted(shown)}"
    )


def test_g_toggle_graph_shown():
    """g (toggle_graph) must appear in the footer. Regression: was show=False."""
    shown = _shown_keys()
    assert "g" in shown, f"'g' (toggle_graph) missing from footer. Shown keys: {sorted(shown)}"


def test_nav_keys_not_cluttering_footer():
    """j/k/up/down/pageup/pagedown must stay hidden — they clutter the footer."""
    from juggle_cockpit import CockpitApp
    hidden_nav = {"j", "k", "up", "down", "pageup", "pagedown"}
    shown = {b.key for b in CockpitApp.BINDINGS if b.show}
    unwanted = hidden_nav & shown
    assert not unwanted, f"Nav keys should stay hidden; found in footer: {unwanted}"
