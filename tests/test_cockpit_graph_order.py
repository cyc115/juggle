"""Unit tests for the cockpit graph-panel ordering + snapshot cache.

Feature: auto-sort the cockpit graph panel so recently-worked projects rise and
finished ones sink and collapse (spec 2026-07-03-cockpit-graph-sort).

Pure tests only — inject ``now`` and the cache, feed ``ProjectActivity`` rows.
No live TUI, no sleep, no DB.
"""
from __future__ import annotations

import pytest

from juggle_cockpit_graph_order import (
    ProjectActivity,
    OrderCache,
    order_projects,
)


def PA(pid, *, done=False, active=0, done_key=0):
    return ProjectActivity(id=pid, is_done=done, active_key=active, done_key=done_key)


# --------------------------------------------------------------------------
# Fresh-order partitioning (interval<=0 => always recompute)
# --------------------------------------------------------------------------

def test_active_projects_sort_above_done():
    rows = [PA("D", done=True, done_key=999), PA("A", active=1)]
    assert order_projects(rows, now=0, interval=0, cache=OrderCache()) == ["A", "D"]


def test_recency_within_active_partition_desc():
    rows = [PA("low", active=1), PA("high", active=9), PA("mid", active=5)]
    assert order_projects(rows, now=0, interval=0, cache=OrderCache()) == [
        "high",
        "mid",
        "low",
    ]


def test_done_partition_sorts_by_completion_recency_desc():
    rows = [PA("old", done=True, done_key=1), PA("new", done=True, done_key=9)]
    assert order_projects(rows, now=0, interval=0, cache=OrderCache()) == ["new", "old"]


def test_tiebreak_by_id_within_active():
    rows = [PA("b", active=5), PA("a", active=5)]
    assert order_projects(rows, now=0, interval=0, cache=OrderCache()) == ["a", "b"]


def test_inbox_pinned_first_even_when_done_and_stale():
    rows = [
        PA("INBOX", done=True, active=0, done_key=0),
        PA("A", active=9),
        PA("D", done=True, done_key=9),
    ]
    assert order_projects(rows, now=0, interval=0, cache=OrderCache()) == [
        "INBOX",
        "A",
        "D",
    ]


# --------------------------------------------------------------------------
# Snapshot cache / throttle
# --------------------------------------------------------------------------

def test_order_frozen_within_interval_even_when_activity_changes():
    cache = OrderCache()
    first = [PA("A", active=1), PA("B", active=2)]
    assert order_projects(first, now=0, interval=60, cache=cache) == ["B", "A"]

    # A becomes the most-active project mid-window; order must NOT reshuffle.
    churned = [PA("A", active=100), PA("B", active=2)]
    assert order_projects(churned, now=30, interval=60, cache=cache) == ["B", "A"]


def test_new_project_appended_at_tail_without_reshuffle_mid_window():
    cache = OrderCache()
    order_projects([PA("A", active=1), PA("B", active=2)], now=0, interval=60, cache=cache)

    with_new = [PA("A", active=1), PA("B", active=2), PA("C", active=999)]
    assert order_projects(with_new, now=10, interval=60, cache=cache) == ["B", "A", "C"]


def test_deleted_project_dropped_from_frozen_order_mid_window():
    cache = OrderCache()
    order_projects(
        [PA("A", active=1), PA("B", active=2), PA("C", active=3)],
        now=0,
        interval=60,
        cache=cache,
    )
    remaining = [PA("A", active=1), PA("C", active=3)]
    assert order_projects(remaining, now=5, interval=60, cache=cache) == ["C", "A"]


def test_interval_honored_recomputes_after_window_expires():
    cache = OrderCache()
    order_projects([PA("A", active=1), PA("B", active=2)], now=0, interval=60, cache=cache)
    churned = [PA("A", active=100), PA("B", active=2)]
    # now - snapshot_ts == interval => window expired => recompute.
    assert order_projects(churned, now=60, interval=60, cache=cache) == ["A", "B"]


def test_interval_non_positive_recomputes_every_call():
    cache = OrderCache()
    order_projects([PA("A", active=1), PA("B", active=2)], now=0, interval=0, cache=cache)
    churned = [PA("A", active=100), PA("B", active=2)]
    assert order_projects(churned, now=0, interval=0, cache=cache) == ["A", "B"]


# --------------------------------------------------------------------------
# Regression pin
# --------------------------------------------------------------------------

def test_regression_cockpit_graph_autosort_active_over_done_and_frozen():
    """Pin: cockpit-graph-autosort (2026-07-03) — a freshly-worked ACTIVE
    project must sit above a finished one, and the order stays frozen within the
    snapshot interval so the panel does not reshuffle on every render."""
    cache = OrderCache()
    rows = [
        PA("done-old", done=True, done_key=5),
        PA("active-hot", active=10),
        PA("done-new", done=True, done_key=50),
    ]
    first = order_projects(rows, now=100, interval=60, cache=cache)
    assert first == ["active-hot", "done-new", "done-old"]
    # Within the window: churn activity, order must not move.
    churn = [
        PA("done-old", done=True, done_key=5),
        PA("active-hot", active=0),
        PA("done-new", done=True, done_key=50),
    ]
    assert order_projects(churn, now=120, interval=60, cache=cache) == first
