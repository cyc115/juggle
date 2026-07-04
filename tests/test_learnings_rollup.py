"""Tests for gl-rollup — the graph-node learnings rollup event + adoption nudge
(spec 2026-07-03 §5 + Final revisions 3/4/7).

Covers:
  * event-kind registration (LEARNINGS_ROLLUP → orchestrator),
  * ``maybe_rollup_learnings``: the per-project COUNT(verified)-vs-mark trigger,
    the ≥10-completion edge, the project-fully-complete sentinel (fires once),
    the "no new learnings → advance mark silently" path, and payload shape,
  * ``nudge_missing_learnings``: the non-blocking adoption nudge for a verified
    topic with dependents but zero learnings + a stub handoff (idempotent).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as t  # noqa: E402
from dbops import event_kinds as ek  # noqa: E402
from juggle_learnings_rollup import (  # noqa: E402
    ROLLUP_THRESHOLD,
    maybe_rollup_learnings,
    nudge_missing_learnings,
)


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


def _verified_task(db, task_id, *, project="P1", topic=None, learnings=None,
                   verified_at="2026-07-03T01:00:00"):
    g.create_task(db, task_id=task_id, project_id=project, title=task_id, prompt="x")
    if topic is not None:
        g.set_task_topic(db, task_id, topic)
    with db._connect() as c:
        c.execute(
            "UPDATE nodes SET state='verified', learnings=?, verified_at=? "
            "WHERE id=? AND kind='task'",
            (learnings, verified_at, task_id),
        )
        c.commit()


def _open_task(db, task_id, *, project="P1", topic=None):
    g.create_task(db, task_id=task_id, project_id=project, title=task_id, prompt="x")
    if topic is not None:
        g.set_task_topic(db, task_id, topic)


def _events(db, kind=ek.LEARNINGS_ROLLUP):
    with db._connect() as c:
        rows = c.execute(
            "SELECT kind, handled_by, message FROM notifications_v2 WHERE kind=?",
            (kind,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── event-kind registration (Final revision 3) ─────────────────────────────────


def test_kind_registered():
    assert ek.LEARNINGS_ROLLUP in ek.ALL_KINDS
    assert ek.handled_by_for_kind(ek.LEARNINGS_ROLLUP) == "orchestrator"
    assert ek.is_pushable(ek.handled_by_for_kind(ek.LEARNINGS_ROLLUP))


# ── rollup trigger ─────────────────────────────────────────────────────────────


def test_emits_on_threshold_completions_with_learnings(db):
    for i in range(ROLLUP_THRESHOLD):
        _verified_task(db, f"n{i}", learnings=("keep flag X" if i == 0 else None))
    maybe_rollup_learnings(db, "P1")
    evs = _events(db)
    assert len(evs) == 1
    assert evs[0]["handled_by"] == "orchestrator"
    assert "n0: keep flag X" in evs[0]["message"]
    # mark advanced to the derived completion count
    assert db.get_setting("learnings_rollup_mark:P1") == str(ROLLUP_THRESHOLD)


def test_silent_advance_when_no_new_learnings(db):
    for i in range(ROLLUP_THRESHOLD):
        _verified_task(db, f"n{i}", learnings=None)
    maybe_rollup_learnings(db, "P1")
    assert _events(db) == []
    assert db.get_setting("learnings_rollup_mark:P1") == str(ROLLUP_THRESHOLD)


def test_no_rollup_below_threshold(db):
    # 5 verified + 5 still open → not complete, < threshold new completions
    for i in range(5):
        _verified_task(db, f"n{i}", learnings="x")
    for i in range(5):
        _open_task(db, f"o{i}")
    maybe_rollup_learnings(db, "P1")
    assert _events(db) == []
    assert db.get_setting("learnings_rollup_mark:P1") in (None, "0")


def test_does_not_refire_without_new_completions(db):
    for i in range(ROLLUP_THRESHOLD):
        _verified_task(db, f"n{i}", learnings=("a" if i == 0 else None))
    maybe_rollup_learnings(db, "P1")
    maybe_rollup_learnings(db, "P1")  # no new completions since mark
    assert len(_events(db)) == 1


# ── project-fully-complete edge (Final revision 4: sentinel fires once) ─────────


def test_fires_on_project_complete_below_threshold(db):
    _verified_task(db, "a", learnings="alpha")
    _verified_task(db, "b", learnings=None)
    maybe_rollup_learnings(db, "P1")
    assert len(_events(db)) == 1
    assert db.get_setting("learnings_rollup_final:P1")


def test_project_complete_fires_once(db):
    _verified_task(db, "a", learnings="alpha")
    maybe_rollup_learnings(db, "P1")
    maybe_rollup_learnings(db, "P1")
    assert len(_events(db)) == 1


# ── adoption nudge (Final revision 7) ──────────────────────────────────────────


def _dep_edge(db, from_task, to_task):
    with db._connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO node_edges (node_id, depends_on_id, kind) "
            "VALUES (?,?,'dep')",
            (from_task, to_task),
        )
        c.commit()


def _verified_topic(db, topic_id, *, project="P1", handoff=None):
    t.create_topic(db, topic_id=topic_id, project_id=project, title=topic_id)
    with db._connect() as c:
        c.execute(
            "UPDATE nodes SET state='verified', handoff=? WHERE id=? AND kind='topic'",
            (handoff, topic_id),
        )
        c.commit()


def _open_items(db):
    return db.get_open_action_items()


def test_nudge_topic_with_dependents_zero_learnings_stub(db):
    # TA (upstream, verified, no learnings, empty handoff) → TB depends on it
    _verified_topic(db, "TA", handoff="")
    t.create_topic(db, topic_id="TB", project_id="P1", title="TB")
    _verified_task(db, "ta1", topic="TA", learnings=None)
    _open_task(db, "tb1", topic="TB")
    _dep_edge(db, "tb1", "ta1")  # TB.tb1 depends on TA.ta1 → TA has a dependent
    nudge_missing_learnings(db, "P1")
    items = [i for i in _open_items(db) if "TA" in i["message"]]
    assert len(items) == 1
    assert items[0]["priority"] == "low"


def test_no_nudge_when_learnings_present(db):
    _verified_topic(db, "TA", handoff="")
    t.create_topic(db, topic_id="TB", project_id="P1", title="TB")
    _verified_task(db, "ta1", topic="TA", learnings="gotcha: X")
    _open_task(db, "tb1", topic="TB")
    _dep_edge(db, "tb1", "ta1")
    nudge_missing_learnings(db, "P1")
    assert [i for i in _open_items(db) if "TA" in i["message"]] == []


def test_no_nudge_when_no_dependents(db):
    _verified_topic(db, "TA", handoff="")
    _verified_task(db, "ta1", topic="TA", learnings=None)
    nudge_missing_learnings(db, "P1")
    assert [i for i in _open_items(db) if "TA" in i["message"]] == []


def test_no_nudge_when_handoff_substantive(db):
    _verified_topic(db, "TA", handoff="Adds cmd_foo(); call with --json for machine output.")
    t.create_topic(db, topic_id="TB", project_id="P1", title="TB")
    _verified_task(db, "ta1", topic="TA", learnings=None)
    _open_task(db, "tb1", topic="TB")
    _dep_edge(db, "tb1", "ta1")
    nudge_missing_learnings(db, "P1")
    assert [i for i in _open_items(db) if "TA" in i["message"]] == []


def test_nudge_idempotent(db):
    _verified_topic(db, "TA", handoff="")
    t.create_topic(db, topic_id="TB", project_id="P1", title="TB")
    _verified_task(db, "ta1", topic="TA", learnings=None)
    _open_task(db, "tb1", topic="TB")
    _dep_edge(db, "tb1", "ta1")
    nudge_missing_learnings(db, "P1")
    nudge_missing_learnings(db, "P1")
    assert len([i for i in _open_items(db) if "TA" in i["message"]]) == 1


# ── graph_tick hook (serialized, watchdog-owned) ───────────────────────────────


def test_graph_tick_invokes_rollup_and_nudge_per_project(db, monkeypatch):
    import juggle_graph_dispatch as gd

    _verified_task(db, "n0", project="P1", learnings=None)  # gives P1 graph work
    seen = {"rollup": [], "nudge": []}
    monkeypatch.setattr(
        "juggle_learnings_rollup.maybe_rollup_learnings",
        lambda d, pid: seen["rollup"].append(pid),
    )
    monkeypatch.setattr(
        "juggle_learnings_rollup.nudge_missing_learnings",
        lambda d, pid: seen["nudge"].append(pid),
    )
    gd.graph_tick(db, dispatch_fn=lambda *a, **k: None)
    assert "P1" in seen["rollup"]
    assert "P1" in seen["nudge"]
