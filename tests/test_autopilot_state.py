"""Tests for juggle_autopilot_state — autopilot global toggle accessors.

P7: per-project arming is REMOVED. arm_project/disarm_project raise RuntimeError.
The module retains get_armed_projects (returns []) and get_armed_project (returns
None) as compat stubs so existing importers don't break on import. The
autopilot_armed_project settings key is preserved as dead data — reads are safe.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
import juggle_autopilot_state as st  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "ap.db"))
    d.init_db()
    return d


def test_empty_and_blank_mean_disarmed(db):
    assert st.get_armed_projects(db) == []
    db.set_setting(st.ARMED_PROJECT_KEY, "  ")
    assert st.get_armed_projects(db) == []


def test_legacy_scalar_reads_as_one_element_set(db):
    """REGRESSION PIN (2026-06-10): scalar→set migration. A DB armed by the
    OLD code path (plain scalar set_setting) must read back as a 1-element
    armed set — backward compat is structural, not migratory."""
    db.set_setting(st.ARMED_PROJECT_KEY, "juggle")
    assert st.get_armed_projects(db) == ["juggle"]
    assert st.get_armed_project(db) == "juggle"  # compat shim


def test_csv_parse_strip_dedupe_order(db):
    db.set_setting(st.ARMED_PROJECT_KEY, " a , b ,a,, c ")
    assert st.get_armed_projects(db) == ["a", "b", "c"]


def test_pre_migration_db_degrades_to_empty(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "raw.db"))  # no init_db → no settings table
    assert st.get_armed_projects(d) == []
    assert st.get_armed_project(d) is None


# ---------------------------------------------------------------------------
# P7 pins — arm/disarm raise RuntimeError
# ---------------------------------------------------------------------------


def test_arm_project_raises_p7(db):
    """REGRESSION PIN (P7): arm_project must raise RuntimeError — arming is gone."""
    with pytest.raises(RuntimeError, match="P7"):
        st.arm_project(db, "proj")


def test_disarm_project_raises_p7(db):
    """REGRESSION PIN (P7): disarm_project must raise RuntimeError — arming is gone."""
    with pytest.raises(RuntimeError, match="P7"):
        st.disarm_project(db, "proj")
