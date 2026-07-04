"""Tests for `juggle graph retry-node` — reset failed/cancelled node → open
(Phase6, 2026-07-03 graph-node-primitives spec §4 + DA "un-cancel").

Covers: allowed source states (failed-verify / blocked-failed / cancelled) reset
to 'open' and clear verify_failure/verify_retries/fail_envelope; guarded refusal
(exit 2) on any non-retryable state; --cascade un-blocks dependents via
recompute_blocked; --json shape; node-not-found (exit 1). Also pins the aligned
mark-task guarded-refusal exit (1 → 2).
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
import juggle_cmd_graph_ops as ops  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


def _mk(db, pid, task_id, title, *, deps=None):
    g.create_task(db, task_id=task_id, project_id=pid, title=title,
                  prompt=f"do {task_id}")
    if deps:
        g.replace_edges(db, task_id, deps)


def _to_failed_verify(db, task_id):
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start", "verify_fail"):
        g.task_transition(db, task_id, ev)


def _args(db, node_id, **kw):
    base = dict(node_id=node_id, cascade=False, json_out=False,
                db_path=str(db.db_path))
    base.update(kw)
    return SimpleNamespace(**base)


# ── source-state guard ──────────────────────────────────────────────────────────


def test_retry_failed_verify_resets_to_open_and_clears_failure(db, capsys):
    _mk(db, "INBOX", "a", "Task A")
    _to_failed_verify(db, "a")
    g.bump_verify_retry(db, "a", "boom traceback")
    g.set_task_fail_envelope(db, "a", '{"reason": "x"}')

    ops.cmd_graph_retry_node(_args(db, "a"))

    task = g.get_task(db, "a")
    assert task["state"] == "open"
    assert task["verify_failure"] is None
    assert task["verify_retries"] == 0
    assert task["fail_envelope"] is None
    assert "retry a" in capsys.readouterr().out


def test_retry_blocked_failed_resets_to_open(db):
    _mk(db, "INBOX", "a", "Task A")
    _mk(db, "INBOX", "b", "Task B", deps=["a"])
    _to_failed_verify(db, "a")
    g.propagate_failure(db, "a")
    assert g.get_task(db, "b")["state"] == "blocked-failed"

    ops.cmd_graph_retry_node(_args(db, "b"))
    assert g.get_task(db, "b")["state"] == "open"


def test_retry_uncancels_cancelled_node(db):
    _mk(db, "INBOX", "a", "Task A")
    g.task_transition(db, "a", "cancel")
    assert g.get_task(db, "a")["state"] == "cancelled"

    ops.cmd_graph_retry_node(_args(db, "a"))
    assert g.get_task(db, "a")["state"] == "open"


def test_retry_refuses_non_retryable_state_exit_2(db, capsys):
    _mk(db, "INBOX", "a", "Task A")  # 'open' — not retryable
    with pytest.raises(SystemExit) as exc:
        ops.cmd_graph_retry_node(_args(db, "a"))
    assert exc.value.code == 2
    assert g.get_task(db, "a")["state"] == "open"  # untouched


def test_retry_missing_node_exit_1(db):
    with pytest.raises(SystemExit) as exc:
        ops.cmd_graph_retry_node(_args(db, "nope"))
    assert exc.value.code == 1


# ── --cascade ────────────────────────────────────────────────────────────────────


def test_retry_cascade_unblocks_dependents(db):
    _mk(db, "INBOX", "a", "Task A")
    _mk(db, "INBOX", "b", "Task B", deps=["a"])
    _mk(db, "INBOX", "c", "Task C", deps=["b"])
    _to_failed_verify(db, "a")
    g.propagate_failure(db, "a")
    assert g.get_task(db, "b")["state"] == "blocked-failed"
    assert g.get_task(db, "c")["state"] == "blocked-failed"

    ops.cmd_graph_retry_node(_args(db, "a", cascade=True))
    assert g.get_task(db, "a")["state"] == "open"
    assert g.get_task(db, "b")["state"] == "open"
    assert g.get_task(db, "c")["state"] == "open"


def test_retry_without_cascade_leaves_dependents_blocked(db):
    _mk(db, "INBOX", "a", "Task A")
    _mk(db, "INBOX", "b", "Task B", deps=["a"])
    _to_failed_verify(db, "a")
    g.propagate_failure(db, "a")

    ops.cmd_graph_retry_node(_args(db, "a"))
    assert g.get_task(db, "a")["state"] == "open"
    assert g.get_task(db, "b")["state"] == "blocked-failed"


# ── --json shape ─────────────────────────────────────────────────────────────────


def test_retry_json_shape(db, capsys):
    _mk(db, "INBOX", "a", "Task A")
    _mk(db, "INBOX", "b", "Task B", deps=["a"])
    _to_failed_verify(db, "a")
    g.propagate_failure(db, "a")

    ops.cmd_graph_retry_node(_args(db, "a", cascade=True, json_out=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["id"] == "a"
    assert payload["state"] == "open"
    assert payload["from"] == "failed-verify"
    assert payload["cascaded"] == ["b"]


def test_retry_json_refusal(db, capsys):
    _mk(db, "INBOX", "a", "Task A")  # 'open'
    with pytest.raises(SystemExit) as exc:
        ops.cmd_graph_retry_node(_args(db, "a", json_out=True))
    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


# ── interface delta: mark-task guarded-refusal exit 1 → 2 ───────────────────────


def test_mark_task_refusal_exits_2(db):
    """A guarded transition refusal (terminal-state task) exits 2, not 1."""
    _mk(db, "INBOX", "a", "Task A")
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start", "integrate_ok"):
        g.task_transition(db, "a", ev)  # → verified (terminal)
    args = SimpleNamespace(task_id="a", fail=False, handoff=None,
                           db_path=str(db.db_path))
    with pytest.raises(SystemExit) as exc:
        ops.cmd_graph_mark_task(args)
    assert exc.value.code == 2
