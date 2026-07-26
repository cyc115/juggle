"""Tests for `juggle graph cancel-node` — the soft-terminal 'cancelled' primitive
(graph-node-primitives Phase5, 2026-07-03 spec §3 + DA-B1/B4 + addendum).

Covers: single-node cancel, idempotent no-op, in-flight/verified refuse (exit 2),
the without-cascade non-terminal-dependent refuse (exit 2), --cascade transitive
subtree closure (visit-once/diamond-safe), the all-or-nothing in-flight guard,
--dry-run (no mutation), --reason persistence, --json shape, and exit codes.
State is written ONLY through db_graph.task_transition (never raw UPDATE).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as t  # noqa: E402
import juggle_cmd_graph_cancel as ops  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


def _mk(db, task_id, title, *, deps=None):
    g.create_task(db, task_id=task_id, project_id="INBOX", title=title,
                  prompt=f"do {task_id}")
    if deps:
        g.replace_edges(db, task_id, deps)


def _to(db, task_id, *events):
    for ev in events:
        g.task_transition(db, task_id, ev)


def _topic_to(db, topic_id, *events):
    for ev in events:
        t.topic_transition(db, topic_id, ev)


def _args(db, node_id, **kw):
    base = dict(id=node_id, cascade=False, reason=None, dry_run=False,
                json_out=False, db_path=str(db.db_path))
    base.update(kw)
    return SimpleNamespace(**base)


def _states(db):
    return {t["id"]: t["state"] for t in g.list_tasks(db, "INBOX")}


# ── single-node cancel ──────────────────────────────────────────────────────────


def test_cancel_open_node_sets_cancelled(db, capsys):
    _mk(db, "a", "Task A")
    ops.cmd_graph_cancel_node(_args(db, "a"))
    out = capsys.readouterr().out
    assert "cancelled a" in out
    assert _states(db)["a"] == "cancelled"


def test_cancel_failed_verify_node(db):
    _mk(db, "a", "Task A")
    _to(db, "a", "deps_ready", "claim", "dispatch", "integrate_start", "verify_fail")
    ops.cmd_graph_cancel_node(_args(db, "a"))
    assert _states(db)["a"] == "cancelled"


# ── idempotency ─────────────────────────────────────────────────────────────────


def test_cancel_idempotent_noop(db, capsys):
    _mk(db, "a", "Task A")
    ops.cmd_graph_cancel_node(_args(db, "a"))
    capsys.readouterr()
    ops.cmd_graph_cancel_node(_args(db, "a"))  # second call: no error, exit 0
    out = capsys.readouterr().out
    assert "already cancelled" in out
    assert _states(db)["a"] == "cancelled"


# ── refuse (exit 2) on in-flight / verified ─────────────────────────────────────


def test_cancel_refuses_running(db):
    _mk(db, "a", "Task A")
    _to(db, "a", "deps_ready", "claim", "dispatch")  # running
    with pytest.raises(SystemExit) as ei:
        ops.cmd_graph_cancel_node(_args(db, "a"))
    assert ei.value.code == 2
    assert _states(db)["a"] == "running"  # unchanged


def test_cancel_refuses_verified(db):
    _mk(db, "a", "Task A")
    _to(db, "a", "deps_ready", "claim", "dispatch", "integrate_start", "integrate_ok")
    with pytest.raises(SystemExit) as ei:
        ops.cmd_graph_cancel_node(_args(db, "a"))
    assert ei.value.code == 2
    assert _states(db)["a"] == "verified"


def test_cancel_node_not_found_exits_1(db):
    with pytest.raises(SystemExit) as ei:
        ops.cmd_graph_cancel_node(_args(db, "ghost"))
    assert ei.value.code == 1


# ── without --cascade: refuse if any dependent is non-terminal ───────────────────


def test_cancel_without_cascade_refuses_nonterminal_dependents(db):
    _mk(db, "a", "Task A")
    _mk(db, "b", "Task B", deps=["a"])  # b is 'open' — non-terminal
    with pytest.raises(SystemExit) as ei:
        ops.cmd_graph_cancel_node(_args(db, "a"))
    assert ei.value.code == 2
    assert _states(db)["a"] == "open"  # untouched


def test_cancel_without_cascade_ok_when_dependents_terminal(db):
    _mk(db, "a", "Task A")
    _mk(db, "b", "Task B", deps=["a"])
    g.task_transition(db, "b", "cancel")  # b terminal (cancelled)
    ops.cmd_graph_cancel_node(_args(db, "a"))  # allowed — no non-terminal dependent
    assert _states(db)["a"] == "cancelled"


# ── --cascade: transitive dependent closure ─────────────────────────────────────


def test_cascade_cancels_whole_subtree(db, capsys):
    # a → {b, c} → d diamond
    _mk(db, "a", "Task A")
    _mk(db, "b", "Task B", deps=["a"])
    _mk(db, "c", "Task C", deps=["a"])
    _mk(db, "d", "Task D", deps=["b", "c"])
    ops.cmd_graph_cancel_node(_args(db, "a", cascade=True))
    out = capsys.readouterr().out
    st = _states(db)
    assert all(st[n] == "cancelled" for n in ("a", "b", "c", "d"))
    assert "+3 dependents" in out


def test_cascade_diamond_visits_each_once(db, capsys):
    _mk(db, "a", "Task A")
    _mk(db, "b", "Task B", deps=["a"])
    _mk(db, "c", "Task C", deps=["a"])
    _mk(db, "d", "Task D", deps=["b", "c"])  # reachable via b and via c
    ops.cmd_graph_cancel_node(_args(db, "a", cascade=True, json_out=True))
    payload = json.loads(capsys.readouterr().out)
    # each affected id appears exactly once despite the diamond
    assert sorted(payload["cancelled"]) == ["a", "b", "c", "d"]
    assert len(payload["cancelled"]) == len(set(payload["cancelled"]))


def test_cascade_all_or_nothing_refuses_on_inflight_dependent(db):
    _mk(db, "a", "Task A")
    _mk(db, "b", "Task B", deps=["a"])
    _to(db, "b", "deps_ready", "claim", "dispatch")  # b running (in-flight)
    with pytest.raises(SystemExit) as ei:
        ops.cmd_graph_cancel_node(_args(db, "a", cascade=True))
    assert ei.value.code == 2
    st = _states(db)
    assert st["a"] == "open" and st["b"] == "running"  # nothing mutated


# ── --dry-run: report, never mutate ─────────────────────────────────────────────


def test_dry_run_reports_without_mutating(db, capsys):
    _mk(db, "a", "Task A")
    _mk(db, "b", "Task B", deps=["a"])
    before = _states(db)
    ops.cmd_graph_cancel_node(_args(db, "a", cascade=True, dry_run=True))
    out = capsys.readouterr().out
    assert "a" in out and "b" in out
    assert _states(db) == before  # zero mutation


# ── --reason persistence ────────────────────────────────────────────────────────


def test_reason_persisted_on_cancelled_node(db):
    _mk(db, "a", "Task A")
    ops.cmd_graph_cancel_node(_args(db, "a", reason="obsolete cluster"))
    assert g.get_task(db, "a")["cancel_reason"] == "obsolete cluster"


# ── owning-topic reconcile (spec regression pin: cancelled drops from counts) ────


def test_cancel_reconciles_owning_topic(db):
    """A cancelled task must re-derive its topic: 'cancelled' is a COMPLETION
    terminal (DA-B1), so a topic whose only task is cancelled leaves the open
    partition (derives to a completion state, never stuck 'open'/'failed')."""
    t.create_topic(db, topic_id="TOP", project_id="INBOX", title="Topic")
    _mk(db, "a", "Task A")
    g.set_task_topic(db, "a", "TOP")  # a is now TOP's member task
    ops.cmd_graph_cancel_node(_args(db, "a"))
    assert _states(db)["a"] == "cancelled"
    # all member tasks terminal-complete → topic derives out of 'open'
    assert t.get_topic(db, "TOP")["state"] in ("integrating", "verified")


# ── topic-level cancel (2026-07-25 CLI GAP: a stuck failed-integration TOPIC
# node had no CLI path to clear it — mark-task/cancel-node both rejected any
# non-task node as "not found". cancel-node is the right lever: its
# soft-terminal 'cancelled' state is kind-agnostic in the state machine (both
# db_graph.task_transition and db_topics.topic_transition delegate to the same
# node_transition("...", "task")), whereas mark-task is per-task COMPLETION
# semantics (verify_ok tri-state, handoff) that don't apply to a topic. ────────


def test_cancel_topic_node_succeeds(db, capsys):
    t.create_topic(db, topic_id="TOP", project_id="INBOX", title="Topic")
    ops.cmd_graph_cancel_node(_args(db, "TOP"))
    out = capsys.readouterr().out
    assert "cancelled TOP" in out
    assert t.get_topic(db, "TOP")["state"] == "cancelled"


def test_cancel_topic_stuck_in_failed_integration_succeeds(db):
    """The exact stuck state from the 2026-07-25 CLI GAP incident: a topic
    parked at 'failed-integration' with no CLI able to clear it."""
    t.create_topic(db, topic_id="TOP", project_id="INBOX", title="Topic")
    _topic_to(db, "TOP", "deps_ready", "claim", "dispatch", "integrate_start", "integrate_fail")
    assert t.get_topic(db, "TOP")["state"] == "failed-integration"
    ops.cmd_graph_cancel_node(_args(db, "TOP"))
    assert t.get_topic(db, "TOP")["state"] == "cancelled"


def test_cancel_topic_refuses_protected_state(db):
    """The in-flight/protected guard must apply to a topic root too — before
    the topic-node fix this check only ever consulted db_graph.get_task
    (task-only), so a topic root silently bypassed it."""
    t.create_topic(db, topic_id="TOP", project_id="INBOX", title="Topic")
    _topic_to(db, "TOP", "deps_ready", "claim", "dispatch")  # running
    with pytest.raises(SystemExit) as ei:
        ops.cmd_graph_cancel_node(_args(db, "TOP"))
    assert ei.value.code == 2
    assert t.get_topic(db, "TOP")["state"] == "running"  # unchanged


def test_cancel_topic_cascade_refused(db):
    """Topic-level deps are DERIVED from underlying task edges, not stored as
    direct node_edges on the topic itself (db_topics module docstring) — the
    task-dependents cascade closure doesn't apply, so --cascade on a topic is
    a guarded refusal (exit 2) rather than silently no-op'ing the cascade."""
    t.create_topic(db, topic_id="TOP", project_id="INBOX", title="Topic")
    with pytest.raises(SystemExit) as ei:
        ops.cmd_graph_cancel_node(_args(db, "TOP", cascade=True))
    assert ei.value.code == 2
    assert t.get_topic(db, "TOP")["state"] == "open"  # untouched


def test_cancel_task_with_orphaned_topic_ref_does_not_crash(db):
    """REGRESSION PIN (2026-07-26): cancel-node mutated the task to 'cancelled'
    and THEN raised an uncaught ValueError out of the parent-topic rollup pass
    (db_topics.reconcile_topic_state) when the task's parent_id referenced a
    topic id that doesn't resolve to an actual topic node — the mutation
    silently applied while the command reported failure. The rollup pass is
    best-effort: a topic ref that doesn't resolve is skipped, never crashes
    after the primary mutation already committed."""
    _mk(db, "a", "Task A")
    g.set_task_topic(db, "a", "GHOST_TOPIC")  # orphaned ref — never created
    ops.cmd_graph_cancel_node(_args(db, "a"))  # must not raise
    assert _states(db)["a"] == "cancelled"


# ── --json shape ────────────────────────────────────────────────────────────────


def test_json_shape_single(db, capsys):
    _mk(db, "a", "Task A")
    ops.cmd_graph_cancel_node(_args(db, "a", json_out=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "id": "a", "cancelled": ["a"]}
