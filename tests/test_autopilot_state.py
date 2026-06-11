"""Tests for juggle_autopilot_state — CSV armed-set accessors (multi-project
autopilot, 2026-06-10). The settings key autopilot_armed_project remains the
SOLE arming authority (DA M6); its value is now an ordered CSV of project ids
(1-element value ≡ the legacy scalar — zero migration)."""
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


def test_arm_appends_idempotently(db):
    assert st.arm_project(db, "a") == ["a"]
    assert st.arm_project(db, "b") == ["a", "b"]
    assert st.arm_project(db, "a") == ["a", "b"]
    assert db.get_setting(st.ARMED_PROJECT_KEY) == "a,b"


def test_arm_rejects_unsafe_ids(db):
    for bad in ("a,b", "a b", " a", ""):
        with pytest.raises(ValueError):
            st.arm_project(db, bad)
    assert st.get_armed_projects(db) == []


def test_disarm_removes_one_keeps_rest(db):
    st.arm_project(db, "a")
    st.arm_project(db, "b")
    assert st.disarm_project(db, "a") == ["b"]
    assert st.disarm_project(db, "a") == ["b"]  # absent → no-op


def test_set_empty_clears_key(db):
    st.arm_project(db, "a")
    st.set_armed_projects(db, [])
    assert db.get_setting(st.ARMED_PROJECT_KEY) is None


def test_pre_migration_db_degrades_to_empty(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "raw.db"))  # no init_db → no settings table
    assert st.get_armed_projects(d) == []
    assert st.get_armed_project(d) is None
