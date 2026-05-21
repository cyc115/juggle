"""Tests for cockpit v2 column-width persistence (column_ratios).

TDD cycles covering:
  1. _write_ratios persists normalized config
  2. normalization always sums to 1.0
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
