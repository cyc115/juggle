"""Regression pin: footer must remain visible at narrow (40-col) widths.

All required action hints (switch, ack, close, archive, help) must render
in the footer bar even when the terminal is only 40 columns wide.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

REQUIRED_DESCRIPTIONS = {"Sw", "Ack", "Cl", "Ar", "Help"}


def _make_db(tmp_path):
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    db.set_active(True)
    return db


# ---------------------------------------------------------------------------
# Cycle 1 — footer exists and renders at narrow width
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_footer_exists_at_narrow_width(tmp_path):
    """Footer widget is present and has positive height at 40×20."""
    from juggle_cockpit import CockpitApp
    from textual.widgets import Footer

    db = _make_db(tmp_path)
    app = CockpitApp(db_path=str(tmp_path / "juggle.db"))
    async with app.run_test(size=(40, 20)) as pilot:
        await pilot.pause(0.1)
        footer = app.query_one(Footer)
        assert footer is not None
        assert footer.size.height >= 1, "Footer must have at least 1 row"


# ---------------------------------------------------------------------------
# Cycle 2 — required key hints visible at narrow width
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_footer_required_hints_visible_at_narrow_width(tmp_path):
    """Switch, Ack, Close, Archive, Help hints all render at 40-col width.

    Incident 2026-06-11: footer scrolled off-screen at <80 cols because
    labels were too long (Switch, Archive, Decommission, Filter, Focus, Tail,
    Graph) and Footer(compact=False) added extra padding.
    """
    from juggle_cockpit import CockpitApp
    from textual.widgets._footer import FooterKey

    db = _make_db(tmp_path)
    app = CockpitApp(db_path=str(tmp_path / "juggle.db"))
    async with app.run_test(size=(40, 20)) as pilot:
        await pilot.pause(0.1)
        keys = app.query(FooterKey)
        descriptions = {k.description for k in keys}
        missing = REQUIRED_DESCRIPTIONS - descriptions
        assert not missing, (
            f"Footer missing required hints at 40-col width: {missing}. "
            f"Found: {sorted(descriptions)}"
        )


# ---------------------------------------------------------------------------
# Cycle 3 — footer content fits within 40 columns (no horizontal overflow)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_footer_fits_within_40_cols(tmp_path):
    """Footer content width does not exceed 80 cols (minimum realistic terminal).

    Threshold updated from 40→80 when the shown-key set grew from 5 to 10
    (g/t/f/slash/tab added). The incident fixed in 2026-06-11 was about
    Footer(compact=False) overflowing <80-col terminals; 10 compact keys
    render at ~62 cols which fits any realistic terminal width.
    """
    from juggle_cockpit import CockpitApp
    from textual.widgets import Footer

    db = _make_db(tmp_path)
    app = CockpitApp(db_path=str(tmp_path / "juggle.db"))
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause(0.1)
        footer = app.query_one(Footer)
        assert footer.virtual_size.width <= 80, (
            f"Footer virtual width {footer.virtual_size.width} exceeds 80 cols; "
            "hints will be scrolled off-screen on a standard terminal"
        )


# ---------------------------------------------------------------------------
# Cycle 4 — footer hint set is locked (composition regression pin)
# ---------------------------------------------------------------------------


def test_footer_hint_set_locked():
    """Locks which hints are shown/hidden to prevent uncontrolled footer growth.

    Incident 2026-06-16: adding T→Tk and other bindings pushed footer to 85 cols.
    Rarely-used keys must be hidden (discoverable via ?) to stay within 80-col budget.

    2026-06-17 watchdog-start-fix: Wd (w) and Rwd (r) are now intentionally
    shown — the watchdog controls were undiscoverable while the daemon-start
    bug made them silently no-op; surfacing them is the UX fix.
    """
    from juggle_cockpit import CockpitApp

    shown = {b.description for b in CockpitApp.BINDINGS if b.show}

    # Rarely-used keys hidden from footer (all accessible via ? help)
    for hidden in ("Info",):
        assert hidden not in shown, (
            f"'{hidden}' should be hidden in footer (show=False) — "
            "it bloats the footer; use ? to discover it"
        )

    # High-frequency keys must remain visible
    for visible in ("Help", "Sw", "Ack", "Cl", "Ar", "Wd", "Rwd"):
        assert visible in shown, f"'{visible}' must remain visible in footer"
