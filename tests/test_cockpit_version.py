"""Cockpit title-bar version display.

The cockpit Header sub_title must surface the juggle version sourced from
.claude-plugin/plugin.json (single source of truth), e.g. "Cockpit v2 · v1.60.1".
The version is read from plugin.json here too so the assertion never drifts.
"""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("rich")
pytest.importorskip("textual")

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

import juggle_cockpit  # noqa: E402


def _plugin_version() -> str:
    pj = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    return json.loads(pj.read_text())["version"]


def test_get_version_matches_plugin_json():
    assert juggle_cockpit._get_version() == _plugin_version()


def test_cockpit_subtitle_contains_version():
    version = _plugin_version()
    sub = juggle_cockpit._cockpit_subtitle(version)
    assert f"v{version}" in sub


def test_cockpit_subtitle_narrow_degrades_to_version_only():
    """At narrow widths the prefix is dropped, but the version stays."""
    version = _plugin_version()
    sub = juggle_cockpit._cockpit_subtitle(version, width=80)
    assert f"v{version}" in sub
    assert "·" not in sub


# ---------------------------------------------------------------------------
# Version-drift banner (TODO L15).
# Incident 2026-06-21: a stale cockpit (v1.77-era) ran ~4.5h while main was
# v1.80.0 and MASKED the leak fix until manual restart. The cockpit must detect
# its boot-captured version vs the on-disk plugin.json version and surface a
# persistent "restart to load" banner when they drift — never auto-restart.
# ---------------------------------------------------------------------------


def test_drift_banner_none_when_boot_equals_current():
    """boot == current → no banner (the common, healthy case)."""
    assert juggle_cockpit._drift_banner("1.85.0", "1.85.0") is None


def test_drift_banner_present_when_patch_version_advanced():
    """boot != current (patch bump on disk) → banner naming both versions."""
    banner = juggle_cockpit._drift_banner("1.85.0", "1.85.1")
    assert banner is not None
    assert "v1.85.0" in banner  # running (boot) version
    assert "v1.85.1" in banner  # available (on-disk) version
    assert "restart" in banner.lower()


def test_drift_banner_none_when_either_version_unknown():
    """A failed read ('?') must NOT raise a false 'restart' banner."""
    assert juggle_cockpit._drift_banner("1.85.0", "?") is None
    assert juggle_cockpit._drift_banner("?", "1.85.1") is None


@pytest.mark.asyncio
async def test_cockpit_no_banner_when_version_unchanged(tmp_path):
    """Render pin: boot == on-disk version → #version-banner stays empty."""
    from juggle_db import JuggleDB
    from textual.widgets import Static

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)

    app = juggle_cockpit.CockpitApp(db_path=db_path)  # boot version == on-disk
    async with app.run_test(size=(160, 40)) as pilot:
        app._refresh()
        await pilot.pause(0.05)
        banner = str(app.query_one("#version-banner", Static).render())
    assert banner == "", f"expected empty banner, got {banner!r}"


@pytest.mark.asyncio
async def test_cockpit_shows_banner_when_disk_version_advances(tmp_path, monkeypatch):
    """Render pin: cockpit booted at vOLD, plugin.json now vNEW → banner shown."""
    from juggle_db import JuggleDB
    from textual.widgets import Static

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)

    # Cockpit boots while on-disk version is the OLD one.
    monkeypatch.setattr(juggle_cockpit, "_get_version", lambda: "1.0.0")
    app = juggle_cockpit.CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        # Disk advances under the running cockpit; the refresh tick re-reads it.
        monkeypatch.setattr(juggle_cockpit, "_get_version", lambda: "1.0.1")
        app._refresh()
        await pilot.pause(0.05)
        banner = str(app.query_one("#version-banner", Static).render())
    assert "v1.0.0" in banner and "v1.0.1" in banner
    assert "restart" in banner.lower()


# ---------------------------------------------------------------------------
# Banner WIDGET visibility (bug 2026-06-29): an empty Static with
# background:$warning still renders a 1-row amber bar. The row must COLLAPSE
# (widget.display False) when there is no drift, and only appear on an upgrade.
# These pin the widget's `display`, not just its text (the existing render-text
# pins above pass even while the amber bar is permanently visible).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cockpit_banner_widget_hidden_when_version_unchanged(tmp_path):
    """boot == on-disk version → #version-banner widget is NOT displayed (no
    amber bar). RED on pre-fix code: the empty widget stays display=True."""
    from juggle_db import JuggleDB
    from textual.widgets import Static

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)

    app = juggle_cockpit.CockpitApp(db_path=db_path)  # boot version == on-disk
    async with app.run_test(size=(160, 40)) as pilot:
        app._refresh()
        await pilot.pause(0.05)
        displayed = app.query_one("#version-banner", Static).display
    assert displayed is False, "empty banner must collapse (no amber bar)"


@pytest.mark.asyncio
async def test_cockpit_banner_widget_shown_on_drift(tmp_path, monkeypatch):
    """boot vOLD, on-disk vNEW → #version-banner widget IS displayed."""
    from juggle_db import JuggleDB
    from textual.widgets import Static

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)

    monkeypatch.setattr(juggle_cockpit, "_get_version", lambda: "1.0.0")
    app = juggle_cockpit.CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        monkeypatch.setattr(juggle_cockpit, "_get_version", lambda: "1.0.1")
        app._refresh()
        await pilot.pause(0.05)
        displayed = app.query_one("#version-banner", Static).display
    assert displayed is True, "drift banner must be visible"
