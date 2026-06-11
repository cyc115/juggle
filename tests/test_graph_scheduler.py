"""juggle_graph_scheduler — least-loaded-first round-robin interleave over
ready TOPICS (R3/R9, spec §2.7). Pure function, no DB. The topic is the
budget unit: one topic = one thread = one agent."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_graph_scheduler import interleave_ready  # noqa: E402


def _t(i):
    return {"id": i}


def test_empty_input():
    assert interleave_ready({}, {}, []) == []


def test_single_project_order_preserved():
    ready = {"p1": [_t("a"), _t("b")]}
    assert interleave_ready(ready, {"p1": 0}, ["p1"]) == [("p1", _t("a")), ("p1", _t("b"))]


def test_round_robin_interleave_two_projects():
    ready = {"p1": [_t("a1"), _t("a2"), _t("a3")], "p2": [_t("b1"), _t("b2")]}
    out = interleave_ready(ready, {"p1": 0, "p2": 0}, ["p1", "p2"])
    assert [(p, t["id"]) for p, t in out] == [
        ("p1", "a1"), ("p2", "b1"), ("p1", "a2"), ("p2", "b2"), ("p1", "a3"),
    ]


def test_least_loaded_project_goes_first():
    """REGRESSION PIN (2026-06-10): with budget 1/tick, arm-order round-robin
    starved every project but the first — least-loaded-first must put the
    project with fewer in-flight topics ahead, statelessly."""
    ready = {"p1": [_t("a1")], "p2": [_t("b1")]}
    out = interleave_ready(ready, {"p1": 2, "p2": 0}, ["p1", "p2"])
    assert [(p, t["id"]) for p, t in out] == [("p2", "b1"), ("p1", "a1")]


def test_tie_break_is_arm_order():
    ready = {"p2": [_t("b1")], "p1": [_t("a1")]}
    out = interleave_ready(ready, {"p1": 1, "p2": 1}, ["p1", "p2"])
    assert [p for p, _ in out] == ["p1", "p2"]


def test_fifty_vs_two_budget_five_prefix_is_fair():
    """Spec §2.7: first 5 interleaved entries contain BOTH small-project topics."""
    ready = {"big": [_t(f"x{i}") for i in range(50)], "small": [_t("s1"), _t("s2")]}
    out = interleave_ready(ready, {"big": 0, "small": 0}, ["big", "small"])
    first5 = [(p, t["id"]) for p, t in out[:5]]
    assert ("small", "s1") in first5 and ("small", "s2") in first5
    assert len(out) == 52


def test_missing_in_flight_defaults_zero_and_empty_projects_skipped():
    assert interleave_ready({"p1": [_t("a")]}, {}, ["p1", "ghost"]) == [("p1", _t("a"))]
    assert interleave_ready({"p1": []}, {"p1": 0}, ["p1"]) == []
