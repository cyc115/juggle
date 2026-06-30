"""Tests for juggle_autopilot_state — default-armed exclusion-set accessors.

2026-06-30 (user-approved restore of per-project arm/disarm): per-project
arming is BACK with a default-armed semantic. The stored authority is a
DISARMED exclusion set (DISARMED_PROJECT_KEY); an empty set means every active
project is armed. arm_project removes from the set, disarm_project adds to it.
These pins replace the P7 pins that asserted arm/disarm raise RuntimeError.
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


# ── disarmed-set CSV accessor (moved parse/validate, same logic, new key) ──────

def test_empty_and_blank_mean_no_disarm(db):
    assert st.get_disarmed_projects(db) == []
    db.set_setting(st.DISARMED_PROJECT_KEY, "  ")
    assert st.get_disarmed_projects(db) == []


def test_csv_parse_strip_dedupe_order(db):
    db.set_setting(st.DISARMED_PROJECT_KEY, " a , b ,a,, c ")
    assert st.get_disarmed_projects(db) == ["a", "b", "c"]


def test_pre_migration_db_degrades_to_empty(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "raw.db"))  # no init_db → no settings table
    assert st.get_disarmed_projects(d) == []
    assert st.get_armed_project(d) is None


def test_set_empty_clears_key(db):
    st.set_disarmed_projects(db, ["x"])
    assert db.get_setting(st.DISARMED_PROJECT_KEY) == "x"
    st.set_disarmed_projects(db, [])
    assert db.get_setting(st.DISARMED_PROJECT_KEY) is None


# ── select_armed primitive (PURE — the single exclusion seam) ──────────────────

def test_select_armed_empty_disarmed_returns_all():
    assert st.select_armed(["a", "b", "c"], []) == ["a", "b", "c"]


def test_select_armed_excludes_and_preserves_order():
    assert st.select_armed(["a", "b", "c", "d"], ["b", "d"]) == ["a", "c"]


def test_select_armed_accepts_set_or_list():
    assert st.select_armed(["a", "b"], {"a"}) == ["b"]


# ── arm/disarm mutate the disarmed exclusion set ───────────────────────────────

def test_disarm_project_adds_to_disarmed(db):
    """REGRESSION PIN (2026-06-30, user-approved restore — replaces P7
    'disarm raises RuntimeError'): disarm_project adds the id to the exclusion
    set so the tick stops driving it."""
    assert st.disarm_project(db, "proj") == ["proj"]
    assert st.get_disarmed_projects(db) == ["proj"]
    assert st.disarm_project(db, "proj") == ["proj"]  # idempotent


def test_arm_project_removes_from_disarmed(db):
    """REGRESSION PIN (2026-06-30, user-approved restore — replaces P7
    'arm raises RuntimeError'): arm_project removes the id from the exclusion
    set (re-arms it); a no-op when already armed."""
    st.set_disarmed_projects(db, ["a", "b"])
    assert st.arm_project(db, "a") == ["b"]
    assert st.get_disarmed_projects(db) == ["b"]
    assert st.arm_project(db, "never-disarmed") == ["b"]  # no-op, no error


def test_arm_all_clears_set(db):
    st.set_disarmed_projects(db, ["a", "b"])
    st.arm_all(db)
    assert st.get_disarmed_projects(db) == []


def test_disarm_all_excludes_every_given_id(db):
    st.disarm_all(db, ["a", "b", "c"])
    assert st.get_disarmed_projects(db) == ["a", "b", "c"]


def test_validate_rejects_bad_ids(db):
    with pytest.raises(ValueError):
        st.disarm_project(db, "has,comma")
    with pytest.raises(ValueError):
        st.disarm_project(db, "has space")


# ── derived armed accessors (default-armed) ────────────────────────────────────

def test_get_armed_projects_default_armed(db):
    """No disarm → every active project is armed (default-armed)."""
    db.create_project(name="Alpha", objective="a")
    db.create_project(name="Beta", objective="b")
    ids = {p["id"] for p in db.list_projects()}
    assert set(st.get_armed_projects(db)) == ids


def test_get_armed_projects_excludes_disarmed(db):
    db.create_project(name="Alpha", objective="a")
    db.create_project(name="Beta", objective="b")
    projects = db.list_projects()
    beta = next(p["id"] for p in projects if p["name"] == "Beta")
    st.disarm_project(db, beta)
    armed = st.get_armed_projects(db)
    assert beta not in armed
    assert len(armed) == len(projects) - 1


def test_get_armed_project_first_or_none(db):
    assert st.get_armed_project(db) in (None, *[p["id"] for p in db.list_projects()])
