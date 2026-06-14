"""TDD tests for cockpit project-arm modal (Feature A).

Tests the pure build_project_arm_rows function and toggle logic.
No live terminal required.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("textual", reason="textual not installed")


# ---------------------------------------------------------------------------
# build_project_arm_rows — pure row builder
# ---------------------------------------------------------------------------

def test_build_rows_armed_flag():
    """Armed projects get armed=True; others get armed=False."""
    from juggle_cockpit_modals import build_project_arm_rows

    projects = [
        {"id": "proj-a", "name": "Alpha"},
        {"id": "proj-b", "name": "Beta"},
    ]
    armed_set = {"proj-a"}
    task_counts = {"proj-a": None, "proj-b": None}

    rows = build_project_arm_rows(projects, armed_set, task_counts)
    assert len(rows) == 2
    assert rows[0].pid == "proj-a"
    assert rows[0].armed is True
    assert rows[1].pid == "proj-b"
    assert rows[1].armed is False


def test_build_rows_graph_progress():
    """X/Y verified/total and running count come from task_counts."""
    from juggle_cockpit_modals import build_project_arm_rows

    projects = [{"id": "p1", "name": "One"}]
    armed_set = set()
    task_counts = {"p1": {"verified": 3, "total": 5, "running": 1}}

    rows = build_project_arm_rows(projects, armed_set, task_counts)
    assert rows[0].verified == 3
    assert rows[0].total == 5
    assert rows[0].running == 1


def test_build_rows_no_graph_hint():
    """Projects with no graph tasks get hint '— no graph'."""
    from juggle_cockpit_modals import build_project_arm_rows

    projects = [{"id": "p1", "name": "One"}]
    task_counts = {"p1": None}

    rows = build_project_arm_rows(projects, set(), task_counts)
    assert rows[0].hint == "— no graph"
    assert rows[0].total == 0


def test_build_rows_complete_hint():
    """Projects with verified==total (and total>0) get hint '(complete)'."""
    from juggle_cockpit_modals import build_project_arm_rows

    projects = [{"id": "p1", "name": "One"}]
    task_counts = {"p1": {"verified": 4, "total": 4, "running": 0}}

    rows = build_project_arm_rows(projects, set(), task_counts)
    assert rows[0].hint == "(complete)"


def test_build_rows_no_hint_when_partial():
    """Projects with partial progress have empty hint."""
    from juggle_cockpit_modals import build_project_arm_rows

    projects = [{"id": "p1", "name": "One"}]
    task_counts = {"p1": {"verified": 2, "total": 5, "running": 1}}

    rows = build_project_arm_rows(projects, set(), task_counts)
    assert rows[0].hint == ""


def test_build_rows_missing_counts_treated_as_no_graph():
    """Task counts key absent for a project → treated as no graph."""
    from juggle_cockpit_modals import build_project_arm_rows

    projects = [{"id": "p1", "name": "One"}]
    task_counts: dict = {}  # no entry for p1

    rows = build_project_arm_rows(projects, set(), task_counts)
    assert rows[0].hint == "— no graph"


# ---------------------------------------------------------------------------
# Toggle logic
# ---------------------------------------------------------------------------

def test_arm_project_sets_global_on(tmp_path, monkeypatch):
    """arm_project followed by _flag_set(True) results in global=ON."""
    from juggle_db import JuggleDB
    from juggle_autopilot_state import arm_project, get_armed_projects
    from juggle_cmd_autopilot import AUTOPILOT_FLAG, _flag_set

    # Redirect flag file to tmp_path so we don't touch ~/.juggle/autopilot
    fake_flag = tmp_path / "autopilot"
    monkeypatch.setattr("juggle_cmd_autopilot.AUTOPILOT_FLAG", fake_flag)

    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    arm_project(db, "proj-a")
    _flag_set(True)

    assert get_armed_projects(db) == ["proj-a"]
    assert fake_flag.exists()


def test_disarm_last_project_sets_global_off(tmp_path, monkeypatch):
    """Disarming the last project with flag_clear clears the global flag."""
    from juggle_db import JuggleDB
    from juggle_autopilot_state import arm_project, disarm_project, get_armed_projects
    from juggle_cmd_autopilot import _flag_set

    fake_flag = tmp_path / "autopilot"
    monkeypatch.setattr("juggle_cmd_autopilot.AUTOPILOT_FLAG", fake_flag)

    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    arm_project(db, "proj-a")
    _flag_set(True)
    assert fake_flag.exists()

    remaining = disarm_project(db, "proj-a")
    if not remaining:
        _flag_set(False)

    assert get_armed_projects(db) == []
    assert not fake_flag.exists()


def test_arm_all_arms_non_complete_projects():
    """arm-all logic arms every project whose graph is not complete."""
    from juggle_cockpit_modals import build_project_arm_rows

    projects = [
        {"id": "p1", "name": "Alpha"},
        {"id": "p2", "name": "Beta"},   # complete
        {"id": "p3", "name": "Gamma"},  # no graph
    ]
    task_counts = {
        "p1": {"verified": 1, "total": 3, "running": 0},
        "p2": {"verified": 3, "total": 3, "running": 0},
        "p3": None,
    }
    # arm-all = projects where hint != "(complete)"
    rows = build_project_arm_rows(projects, set(), task_counts)
    arm_all_targets = [r.pid for r in rows if r.hint != "(complete)"]
    assert "p1" in arm_all_targets
    assert "p3" in arm_all_targets
    assert "p2" not in arm_all_targets


# ---------------------------------------------------------------------------
# Modal importability and action existence
# ---------------------------------------------------------------------------

def test_project_arm_modal_importable():
    """_ProjectArmModal is importable from juggle_cockpit_modals."""
    from juggle_cockpit_modals import _ProjectArmModal  # noqa: F401
    assert _ProjectArmModal is not None


def test_cockpit_has_action_projects():
    """CockpitApp has action_projects method."""
    from juggle_cockpit import CockpitApp
    assert hasattr(CockpitApp, "action_projects")


def test_p_binding_registered():
    """'p' binding exists in CockpitApp.BINDINGS with action='projects'."""
    from juggle_cockpit import CockpitApp
    bindings = {b.key: b for b in CockpitApp.BINDINGS}
    assert "p" in bindings, "Missing 'p' binding"
    assert bindings["p"].action == "projects"
    assert bindings["p"].show is True
