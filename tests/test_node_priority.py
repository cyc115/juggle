"""Regression pins for fix/defect dispatch priority (T-fix-priority-dispatch-ordering).

USER DIRECTIVE (2026-07-02): fix/defect tasks must outrank feature tasks in the
ready-dispatch order, deterministically in code (not by convention). A fix task
filed AFTER feature tasks must still dispatch first.

Mechanism under test:
  * nodes.priority (int, default 0) — persisted per node, migration-added.
  * `graph add-task --priority N` sets it; id starting with 'fix-' defaults high.
  * interleave_ready() stable pre-sorts each project's ready queue by priority
    DESC (the single dispatch-ordering source) — cross-project fairness unchanged.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as t  # noqa: E402
import juggle_cmd_graph as cg  # noqa: E402
import juggle_graph_add as up  # noqa: E402
from juggle_graph_scheduler import interleave_ready  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


# ── scheduler: within-project priority pre-sort ────────────────────────────────


def test_interleave_sorts_priority_desc_within_project():
    """REGRESSION PIN: feature filed first, fix filed second — fix dispatches first."""
    ready = {
        "p1": [
            {"id": "feat", "priority": 0},
            {"id": "fix", "priority": 100},
        ]
    }
    out = interleave_ready(ready, {"p1": 0}, ["p1"])
    assert [t["id"] for _, t in out] == ["fix", "feat"]


def test_interleave_priority_sort_is_stable_within_a_tier():
    """Equal priority keeps insertion (created_at,id) order — stable pre-sort."""
    ready = {"p1": [{"id": "a", "priority": 0}, {"id": "b", "priority": 0}]}
    out = interleave_ready(ready, {"p1": 0}, ["p1"])
    assert [t["id"] for _, t in out] == ["a", "b"]


def test_interleave_missing_priority_defaults_zero():
    """Topic dicts without a priority key (legacy/synthetic) sort as 0, no crash."""
    ready = {"p1": [{"id": "a"}, {"id": "b", "priority": 5}]}
    out = interleave_ready(ready, {"p1": 0}, ["p1"])
    assert [t["id"] for _, t in out] == ["b", "a"]


def test_interleave_cross_project_fairness_unchanged():
    """Least-loaded-first ordering across projects is untouched by the pre-sort."""
    ready = {"p1": [{"id": "a1"}], "p2": [{"id": "b1"}]}
    out = interleave_ready(ready, {"p1": 2, "p2": 0}, ["p1", "p2"])
    assert [p for p, _ in out] == ["p2", "p1"]


# ── persistence: priority stored on nodes and surfaced ─────────────────────────


def test_create_task_persists_priority(db):
    g.create_task(db, task_id="x", project_id="INBOX", title="X", prompt="do x",
                  priority=7)
    assert g.get_task(db, "x")["priority"] == 7


def test_create_task_priority_defaults_zero(db):
    g.create_task(db, task_id="x", project_id="INBOX", title="X", prompt="do x")
    assert g.get_task(db, "x")["priority"] == 0


def test_create_topic_persists_priority(db):
    t.create_topic(db, topic_id="T-x", project_id="INBOX", title="X", priority=9)
    assert t.get_topic(db, "T-x")["priority"] == 9
    assert t.list_topics(db, "INBOX")[0]["priority"] == 9


# ── add_task propagates priority to the task AND its auto-created topic ─────────


def test_add_task_propagates_priority_to_auto_topic(db):
    res = up.add_task(
        db, "INBOX", task_id="fix-bug", title="Fix", prompt="fix it",
        deps=[], required_by=[], verify_cmd=None,
        topic_id="T-fix-bug", auto_create_topic=True, priority=100,
    )
    assert res["task_id"] == "fix-bug"
    assert g.get_task(db, "fix-bug")["priority"] == 100
    assert t.get_topic(db, "T-fix-bug")["priority"] == 100


# ── CLI: 'fix-' id defaults to high priority with zero new flags ───────────────


def _add_args(db, task_id, title, priority=None):
    return SimpleNamespace(
        project="INBOX", id=task_id, title=title, prompt="do it",
        deps=None, required_by=None, topic=None, priority=priority,
        json_out=True, db_path=str(db.db_path),
    )


def test_cli_fix_prefix_defaults_high_priority(db):
    cg.cmd_graph_add_task(_add_args(db, "fix-crash", "Fix crash"))
    assert g.get_task(db, "fix-crash")["priority"] > 0
    assert t.get_topic(db, "T-fix-crash")["priority"] > 0


def test_cli_feature_id_defaults_zero_priority(db):
    cg.cmd_graph_add_task(_add_args(db, "add-feature", "Feature"))
    assert g.get_task(db, "add-feature")["priority"] == 0


def test_cli_explicit_priority_overrides_fix_default(db):
    cg.cmd_graph_add_task(_add_args(db, "fix-x", "Fix X", priority=3))
    assert g.get_task(db, "fix-x")["priority"] == 3


# ── end-to-end: feature filed first, fix second → fix dispatches first ─────────


def test_end_to_end_fix_outranks_earlier_feature(db):
    """Feature topic created first, fix topic second; interleave over the ready
    topics (in list_topics order) still emits the fix first."""
    cg.cmd_graph_add_task(_add_args(db, "add-feature", "Feature"))
    cg.cmd_graph_add_task(_add_args(db, "fix-bug", "Fix"))
    t.recompute_topic_ready(db, "INBOX")
    topics = t.list_topics(db, "INBOX")
    ready = {"INBOX": [tp for tp in topics if tp["state"] == "ready"]}
    out = interleave_ready(ready, {"INBOX": 0}, ["INBOX"])
    assert out, "expected at least one ready topic"
    assert out[0][1]["id"] == "T-fix-bug"
