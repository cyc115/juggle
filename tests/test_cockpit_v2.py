"""Tests for cockpit v2 column-width persistence (column_ratios).

TDD cycles covering:
  1. _write_ratios persists normalized config
  2. normalization always sums to 1.0
  6. palette_close_preserves_layout — on_resize "wide" must not reset dragged widths
  3. integer vs float widths both handled
  4. missing config file → no exception, no write
  5. lifecycle: exit() triggers _persist_ratios via Textual Pilot
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Test 1 — _write_ratios persists normalized config
# ---------------------------------------------------------------------------


def test_persist_ratios_writes_normalized_config(tmp_path):
    """_compute_ratios + _write_ratios stores correct column_ratios to config.json."""
    from juggle_cockpit_v2 import _compute_ratios, _write_ratios

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text('{"cockpit": {"column_ratios": [0.18, 0.62, 0.20]}}')

    ratios = _compute_ratios(33, 33, 34)
    _write_ratios(cfg_file, ratios)

    result = json.loads(cfg_file.read_text())
    stored = result["cockpit"]["column_ratios"]
    assert len(stored) == 3
    assert stored[0] == pytest.approx(0.33, abs=0.01)
    assert stored[1] == pytest.approx(0.33, abs=0.01)
    assert stored[2] == pytest.approx(0.34, abs=0.01)


# ---------------------------------------------------------------------------
# Test 2 — normalization always sums to 1.0
# ---------------------------------------------------------------------------


def test_persist_ratios_normalization_sums_to_one():
    """Normalized ratios always sum to 1.0 (last element absorbs rounding error)."""
    from juggle_cockpit_v2 import _compute_ratios

    ratios = _compute_ratios(33, 33, 34)
    assert sum(ratios) == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# Test 3 — integer vs float widths both handled
# ---------------------------------------------------------------------------


def test_persist_ratios_handles_integer_and_float_widths():
    """_compute_ratios handles both integer cell counts (post-drag) and floats."""
    from juggle_cockpit_v2 import _compute_ratios

    int_ratios = _compute_ratios(33, 33, 34)
    float_ratios = _compute_ratios(33.0, 33.0, 34.0)

    assert int_ratios == float_ratios
    assert sum(int_ratios) == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# Test 4 — missing config file → no exception, no write
# ---------------------------------------------------------------------------


def test_persist_ratios_missing_config_no_exception(tmp_path):
    """_write_ratios is a no-op when config.json does not exist."""
    from juggle_cockpit_v2 import _write_ratios

    missing = tmp_path / "nonexistent.json"
    assert not missing.exists()

    _write_ratios(missing, [0.33, 0.33, 0.34])

    assert not missing.exists()


# ---------------------------------------------------------------------------
# Test 5 — lifecycle: q-press triggers _persist_ratios via exit() (Textual Pilot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_ratios_on_quit(tmp_path, monkeypatch):
    """Pressing q calls exit() which triggers _persist_ratios, writing config.json."""
    from juggle_db import JuggleDB
    from juggle_cockpit_v2 import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text('{"cockpit": {"column_ratios": [0.30, 0.40, 0.30]}}')
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(cfg_file))

    app = CockpitApp(db_path=db_path)
    async with app.run_test() as pilot:
        await pilot.press("q")

    result = json.loads(cfg_file.read_text())
    ratios = result["cockpit"]["column_ratios"]
    assert len(ratios) == 3
    assert abs(sum(ratios) - 1.0) < 0.01


# ---------------------------------------------------------------------------
# Test 6 — palette close preserves layout (regression: on_resize "wide" reset)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_palette_close_preserves_layout(tmp_path, monkeypatch):
    """Spurious Resize after palette close must NOT reset user-dragged column widths.

    Root cause: on_resize "wide" branch unconditionally overwrote #topics/#right with
    config percentages, then left #actions/#agents at stale absolute pixels — giving the
    corrupted [0.22, 0.65, 0.04] layout observed in real terminals.

    Fix: only reset widths when transitioning narrow/medium → wide (topics was hidden).
    """
    from juggle_db import JuggleDB
    from juggle_cockpit_v2 import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)

    # Control config ratios so the test is deterministic
    monkeypatch.setattr("juggle_cockpit_v2._COL_RATIOS", [0.50, 0.30, 0.20])

    app = CockpitApp(db_path=db_path)
    # Terminal wide enough to be "wide" breakpoint (pick_breakpoint: width >= 130)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause(0.1)

        topics = app.query_one("#topics")
        right = app.query_one("#right")
        actions = app.query_one("#actions")
        agents = app.query_one("#agents")

        # Simulate user drag: custom widths differ from config [0.50, 0.30, 0.20]
        # Custom: topics=140 (~70%), right=59 (~30%), actions=35, agents=23
        topics.styles.width = 140
        right.styles.width = 59
        actions.styles.width = 35
        agents.styles.width = 23
        await pilot.pause(0.05)

        # Open palette then close (no resize in headless — exercises the hook path)
        await pilot.press("ctrl+p")
        await pilot.pause(0.2)
        await pilot.press("escape")
        await pilot.pause(0.1)

        # Trigger the spurious Resize that real terminals emit on palette close
        await pilot.resize_terminal(200, 40)
        await pilot.pause(0.1)

        # Custom dragged widths must be preserved (not reset to config 50/30/20)
        assert topics.size.width == 140, (
            f"#topics reset by on_resize! got {topics.size.width}, expected 140"
        )
        assert actions.size.width == 35, (
            f"#actions changed after resize: {actions.size.width}, expected 35"
        )
        assert agents.size.width == 23, (
            f"#agents changed after resize: {agents.size.width}, expected 23"
        )
