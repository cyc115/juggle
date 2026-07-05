"""P1 delivered-dep-readiness pins (loop-entity V2, spec §3).

Incident/change (2026-07-05): multi-topic *deliver* loops terminate their
upstream topics at ``delivered`` (no merge), but every dep-readiness predicate
hard-coded ``state != 'verified'`` — so a ``delivered`` upstream never satisfied
its dependents and the downstream topic/task wedged permanently (silent loop
death, spec §3.1). Fix: widen the FOUR executed READ predicates
(``db_graph.ready_eligible``, ``db_graph_edges.unverified_deps``,
``db_topics.topic_ready_eligible`` — the load-bearing cross-topic one — and the
in-txn readiness in ``juggle_add_node``) from ``!= 'verified'`` to
``NOT IN ('verified','delivered')``, plus their catalog mirror in
``dbops.terminal_states``.

The merge WRITE literal (``orphan_reconcile``'s
``UPDATE nodes SET merged_sha=?, state='verified'``) is NOT widened — it SETS
``state='verified'`` and must stay verified-only (widening it would corrupt the
merge write, spec §3.3).
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
from dbops import terminal_states as ts  # noqa: E402

_SRC = Path(__file__).resolve().parent.parent / "src"

# The FOUR executed READ predicates that P1 widens (must stay in lockstep with
# the dbops.terminal_states catalog mirror).
_EXECUTED_PRED_FILES = (
    "dbops/db_graph.py",
    "dbops/db_graph_edges.py",
    "dbops/db_topics.py",
    "juggle_add_node.py",
)
# The merge WRITE literal that must NOT be widened.
_WRITE_LITERAL_FILE = "dbops/orphan_reconcile.py"


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "delivered.db"))
    d.init_db()
    return d


def _set_state(db, node_id: str, state: str) -> None:
    """Directly stamp a node's state (isolate the readiness predicate from the
    full state-machine dance — 'delivered' is a legal stored terminal)."""
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET state=? WHERE id=?", (state, node_id))


def _mk_task(db, task_id, deps=(), project_id="INBOX"):
    g.create_task(db, task_id=task_id, project_id=project_id,
                  title=task_id, prompt=f"do {task_id}")
    if deps:
        g.replace_edges(db, task_id, list(deps))


def _topic(db, tid, project="INBOX"):
    t.create_topic(db, topic_id=tid, project_id=project, title=f"Topic {tid}",
                   objective="")


def _task_in_topic(db, nid, topic_id, deps=()):
    g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt=f"do {nid}")
    g.set_task_topic(db, nid, topic_id)
    if deps:
        g.replace_edges(db, nid, list(deps))


# ── delivered satisfies (RED pre-fix) ────────────────────────────────────────


def test_delivered_satisfies_node_dep(db):
    """delivered-satisfies-node-dep (2026-07-05): a task whose only dep upstream
    is ``delivered`` becomes ready. Pre-fix ``d.state != 'verified'`` left the
    dependent blocked (db_graph.ready_eligible)."""
    _mk_task(db, "up")
    _mk_task(db, "down", deps=("up",))
    _set_state(db, "up", "delivered")
    assert "down" in g.ready_eligible(db, "INBOX")


def test_delivered_not_reported_as_unverified_dep(db):
    """delivered-satisfies (unverified_deps seam, 2026-07-05): a ``delivered``
    dep is no longer enumerated as a blocking unverified dep
    (db_graph_edges.unverified_deps). Pre-fix it returned ['up'] → RED."""
    _mk_task(db, "up")
    _mk_task(db, "down", deps=("up",))
    _set_state(db, "up", "delivered")
    assert g.unverified_deps(db, "down") == []


def test_delivered_satisfies_cross_topic_dep(db):
    """delivered-satisfies-cross-topic-dep (2026-07-05, LOAD-BEARING): downstream
    topic A's cross-topic dep on upstream topic B is satisfied once B reaches
    ``delivered`` (db_topics.topic_ready_eligible — the cross-topic predicate a
    multi-topic *deliver* loop rides). Pre-fix a delivered deliver-topic never
    unlocked its dependents → the loop iteration wedged."""
    _topic(db, "A")
    _topic(db, "B")
    _task_in_topic(db, "b1", "B")
    _task_in_topic(db, "a1", "A", deps=("b1",))
    # B still open → A blocked on B (only B is eligible)
    assert "A" not in t.topic_ready_eligible(db, "INBOX")
    _set_state(db, "B", "delivered")
    assert t.topic_ready_eligible(db, "INBOX") == ["A"]


# ── guards: not over-widened / not narrowed ──────────────────────────────────


def test_verified_still_satisfies_node_dep(db):
    """verified-still-satisfies (2026-07-05 guard): widening to
    NOT IN ('verified','delivered') must not drop the original ``verified``
    success case."""
    _mk_task(db, "up")
    _mk_task(db, "down", deps=("up",))
    _set_state(db, "up", "verified")
    assert "down" in g.ready_eligible(db, "INBOX")


def test_running_upstream_still_blocks(db):
    """running-still-blocks (2026-07-05 guard): a non-terminal ``running``
    upstream must STILL block its dependent — the widen adds only ``delivered``,
    it must not make every state satisfy (guards against over-widening)."""
    _mk_task(db, "up")
    _mk_task(db, "down", deps=("up",))
    _set_state(db, "up", "running")
    assert "down" not in g.ready_eligible(db, "INBOX")
    assert g.unverified_deps(db, "down") == ["up"]


# ── source ⟺ mirror lockstep ─────────────────────────────────────────────────


def test_executed_predicates_match_mirror():
    """executed-predicates-match-mirror (2026-07-05): the FOUR executed
    dep-readiness predicates are widened to NOT IN ('verified','delivered') AND
    stay in lockstep with the dbops.terminal_states catalog mirror. Pre-fix the
    source still reads ``!= 'verified'`` → RED."""
    scanned = ts.scan_verified_sql_sites(_SRC)
    for f in _EXECUTED_PRED_FILES:
        assert f in scanned, f"{f} dropped from the SQL literal scan"
        assert scanned[f] == ts.VERIFIED_SQL_LITERAL_SITES[f], (
            f"{f} executed predicate disagrees with the terminal_states mirror"
        )
        for lit in scanned[f]:
            assert "NOT IN ('verified','delivered')" in lit, (
                f"{f} dep predicate not widened for delivered: {lit}"
            )


def test_write_literal_unchanged_verified_only():
    """:181-write-unchanged (2026-07-05 guard): the merge WRITE literal
    (orphan_reconcile's ``UPDATE nodes SET merged_sha=?, state='verified'`` — the
    terminal_states mirror line historically cited at :181) is NOT widened; it
    SETS verified and must stay verified-only (widening it would corrupt the
    merge write, spec §3.3)."""
    write_lit = ts.VERIFIED_SQL_LITERAL_SITES[_WRITE_LITERAL_FILE]
    assert write_lit == (
        "\"UPDATE nodes SET merged_sha=?, state='verified', updated_at=? WHERE id=?\",",
    )
    for lit in write_lit:
        assert "state='verified'" in lit
        assert "delivered" not in lit
    # the mirror still matches the live source (never drifted to delivered)
    scanned = ts.scan_verified_sql_sites(_SRC)
    assert scanned[_WRITE_LITERAL_FILE] == write_lit
