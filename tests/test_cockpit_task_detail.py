"""TDD tests for the T hotkey task-detail modal feature.

Covers:
  1. resolve_task_detail — pure resolver (exact id, prefix, label, case, not-found, ambiguous)
  2. BINDINGS — "T" / "shift+t" mapped to task_detail action
  3. action_task_detail method present on CockpitApp
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task(id_, label=None, deps=None):
    t = {"id": id_, "title": f"title-{id_}", "state": "pending", "deps": deps or []}
    if label is not None:
        t["_label"] = label
    return t


# ---------------------------------------------------------------------------
# Cycle 1 — resolve_task_detail pure resolver
# ---------------------------------------------------------------------------


def test_resolve_exact_id():
    """Returns (task, deps) for an exact task-id match."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("AA"), _task("BB")]
    result = resolve_task_detail(tasks, "AA")
    assert result is not None
    task, deps = result
    assert task["id"] == "AA"
    assert deps == []


def test_resolve_exact_id_case_insensitive():
    """Exact id match is case-insensitive."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("AA"), _task("BB")]
    result = resolve_task_detail(tasks, "aa")
    assert result is not None
    assert result[0]["id"] == "AA"


def test_resolve_unique_prefix():
    """Returns task for unique prefix match when no exact id exists."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("ABC"), _task("XYZ")]
    result = resolve_task_detail(tasks, "AB")
    assert result is not None
    assert result[0]["id"] == "ABC"


def test_resolve_exact_beats_prefix():
    """Exact match wins over a prefix match for the same query string."""
    from juggle_cockpit_modals import resolve_task_detail

    # "AB" is both the exact id of one task and a prefix of another
    tasks = [_task("AB"), _task("ABC")]
    result = resolve_task_detail(tasks, "AB")
    assert result is not None
    assert result[0]["id"] == "AB"


def test_resolve_ambiguous_prefix_returns_none():
    """Ambiguous prefix (matches multiple tasks) returns None."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("ABC"), _task("ABD")]
    result = resolve_task_detail(tasks, "AB")
    assert result is None


def test_resolve_label_match():
    """Matches via _label field (thread user_label slug)."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("T1", label="AI"), _task("T2", label="BC")]
    result = resolve_task_detail(tasks, "AI")
    assert result is not None
    assert result[0]["id"] == "T1"


def test_resolve_label_case_insensitive():
    """Label match is case-insensitive."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("T1", label="AI")]
    assert resolve_task_detail(tasks, "ai") is not None
    assert resolve_task_detail(tasks, "Ai") is not None


def test_resolve_not_found_returns_none():
    """Returns None when query matches nothing."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("AA"), _task("BB")]
    assert resolve_task_detail(tasks, "ZZ") is None


def test_resolve_empty_tasks_returns_none():
    """Returns None for empty task list."""
    from juggle_cockpit_modals import resolve_task_detail

    assert resolve_task_detail([], "AA") is None


def test_resolve_deps_from_task_field():
    """Returns deps list from the matched task's own deps field."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("AA", deps=["BB", "CC"])]
    result = resolve_task_detail(tasks, "AA")
    assert result is not None
    assert result[1] == ["BB", "CC"]


def test_resolve_prefix_case_insensitive():
    """Prefix match is case-insensitive."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("ABC")]
    result = resolve_task_detail(tasks, "ab")
    assert result is not None
    assert result[0]["id"] == "ABC"


# ---------------------------------------------------------------------------
# Cycle 2 — BINDINGS: "T" or "shift+t" maps to task_detail
# ---------------------------------------------------------------------------


def _task_detail_binding():
    from juggle_cockpit import CockpitApp
    for b in CockpitApp.BINDINGS:
        if b.action == "task_detail":
            return b
    return None


def test_task_detail_binding_exists():
    """CockpitApp.BINDINGS must have a binding for action 'task_detail'."""
    b = _task_detail_binding()
    assert b is not None, "No binding with action='task_detail' found in CockpitApp.BINDINGS"


def test_task_detail_binding_key():
    """The task_detail binding uses key 'T' or 'shift+t'."""
    b = _task_detail_binding()
    assert b is not None
    assert b.key in ("T", "shift+t"), (
        f"Expected 'T' or 'shift+t', got '{b.key}'"
    )


def test_task_detail_binding_after_tail():
    """task_detail binding appears after the 't' (tail_toggle) binding."""
    from juggle_cockpit import CockpitApp

    keys = [b.key for b in CockpitApp.BINDINGS]
    assert "t" in keys, "tail_toggle binding ('t') not found"
    tail_idx = keys.index("t")
    td_b = _task_detail_binding()
    assert td_b is not None
    td_idx = keys.index(td_b.key)
    assert td_idx > tail_idx, (
        f"task_detail binding at index {td_idx} should come after tail at {tail_idx}"
    )


# ---------------------------------------------------------------------------
# Cycle 3 — action_task_detail method present
# ---------------------------------------------------------------------------


def test_action_task_detail_method_exists():
    """CockpitApp must have an action_task_detail method."""
    from juggle_cockpit import CockpitApp
    assert hasattr(CockpitApp, "action_task_detail"), (
        "CockpitApp missing action_task_detail method"
    )
    assert callable(CockpitApp.action_task_detail)
