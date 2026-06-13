"""Tests for dbops.db_graph — graph_tasks/graph_edges plan store (project autopilot Phase 1).

Covers: schema/migration, CRUD, the task state machine (every legal transition
plus fail-loud illegal ones), ready-set recompute, and completion marking
(DA B3: integrate failure must never yield 'verified'). Cycle detection moved
to juggle_cmd_graph (load-time validation) — see test_cmd_graph.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


def _mk(db, task_id, deps=(), project_id="INBOX", **kw):
    g.create_task(
        db,
        task_id=task_id,
        project_id=project_id,
        title=kw.get("title", task_id),
        prompt=kw.get("prompt", f"do {task_id}"),
        verify_cmd=kw.get("verify_cmd"),
    )
    if deps:
        g.replace_edges(db, task_id, list(deps))


# ── schema ─────────────────────────────────────────────────────────────────────


def test_init_db_creates_graph_tables(db):
    with db._connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "graph_tasks" in tables
    assert "graph_edges" in tables


def test_migration_adds_graph_tables_to_existing_db(tmp_path):
    """Migration 35: pre-existing DB without graph tables gains them on init_db."""
    import sqlite3

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, topic TEXT)")
    conn.commit()
    conn.close()
    d = JuggleDB(db_path=str(db_path))
    d.init_db()
    with d._connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"graph_tasks", "graph_edges"} <= tables


# ── CRUD ───────────────────────────────────────────────────────────────────────


def test_create_and_get_task(db):
    _mk(db, "n1", title="Task One", prompt="build it", verify_cmd="pytest -q")
    task = g.get_task(db, "n1")
    assert task["id"] == "n1"
    assert task["title"] == "Task One"
    assert task["prompt"] == "build it"
    assert task["verify_cmd"] == "pytest -q"
    assert task["state"] == "pending"
    assert task["thread_id"] is None
    assert task["verified_at"] is None


def test_get_task_missing_returns_none(db):
    assert g.get_task(db, "nope") is None


def test_duplicate_create_raises(db):
    _mk(db, "n1")
    with pytest.raises(Exception):
        g.create_task(db, task_id="n1", project_id="INBOX", title="x", prompt="y")


def test_list_tasks_scoped_to_project(db):
    p1 = db.create_project("P1", objective="test")
    _mk(db, "a", project_id="INBOX")
    _mk(db, "b", project_id=p1)
    assert [n["id"] for n in g.list_tasks(db, "INBOX")] == ["a"]
    assert [n["id"] for n in g.list_tasks(db, p1)] == ["b"]


def test_edges_replace_and_get(db):
    _mk(db, "a")
    _mk(db, "b")
    _mk(db, "c", deps=["a", "b"])
    assert sorted(g.get_deps(db, "c")) == ["a", "b"]
    g.replace_edges(db, "c", ["a"])
    assert g.get_deps(db, "c") == ["a"]


def test_set_thread_and_handoff_do_not_touch_state(db):
    _mk(db, "n1")
    g.set_task_thread(db, "n1", "thread-uuid")
    g.set_task_handoff(db, "n1", '{"files": ["x.py"]}')
    task = g.get_task(db, "n1")
    assert task["thread_id"] == "thread-uuid"
    assert task["handoff"] == '{"files": ["x.py"]}'
    assert task["state"] == "pending"


def test_get_task_by_thread(db):
    _mk(db, "n1")
    g.set_task_thread(db, "n1", "t-123")
    assert g.get_task_by_thread(db, "t-123")["id"] == "n1"
    assert g.get_task_by_thread(db, "t-999") is None


# ── state machine ──────────────────────────────────────────────────────────────

LEGAL_CHAIN = [
    ("deps_ready", "ready"),
    ("claim", "dispatching"),
    ("dispatch", "running"),
    ("integrate_start", "integrating"),
    ("integrate_ok", "verified"),
]


def test_full_legal_happy_path(db):
    _mk(db, "n1")
    for event, expected in LEGAL_CHAIN:
        assert g.task_transition(db, "n1", event) == expected
    task = g.get_task(db, "n1")
    assert task["state"] == "verified"
    assert task["verified_at"]  # stored, never inferred from thread status


@pytest.mark.parametrize(
    "path,event,expected",
    [
        (["deps_ready", "claim"], "stale_reset", "ready"),  # crash-safe sweep
        (["deps_ready", "claim", "dispatch"], "exec_fail", "failed-exec"),
        (
            ["deps_ready", "claim", "dispatch", "integrate_start"],
            "integrate_fail",
            "failed-integration",
        ),
        (
            ["deps_ready", "claim", "dispatch", "integrate_start"],
            "verify_fail",
            "failed-verify",
        ),
        ([], "dep_fail", "blocked-failed"),
        (["deps_ready"], "dep_fail", "blocked-failed"),
        # DA round-2 BLOCKER-1 (2026-06-10): blocked-failed had NO outgoing
        # transition — reload of a fixed spec left the blocked tail dead forever.
        (["dep_fail"], "reload", "pending"),
        (["deps_ready", "dep_fail"], "reload", "pending"),
    ],
)
def test_legal_failure_and_reset_transitions(db, path, event, expected):
    _mk(db, "n1")
    for ev in path:
        g.task_transition(db, "n1", ev)
    assert g.task_transition(db, "n1", event) == expected
    assert g.get_task(db, "n1")["state"] == expected


@pytest.mark.parametrize(
    "path,bad_event",
    [
        ([], "claim"),  # pending cannot be claimed
        ([], "integrate_ok"),  # pending cannot complete
        (["deps_ready"], "dispatch"),  # ready cannot skip claim
        (["deps_ready", "claim", "dispatch", "integrate_start", "integrate_ok"], "claim"),
        (["deps_ready", "claim", "dispatch", "integrate_start", "integrate_ok"], "deps_ready"),
        (["deps_ready", "claim", "dispatch", "exec_fail"], "integrate_ok"),
        # blocked-failed resumes ONLY via 'reload' (DA round-2 BLOCKER-1,
        # 2026-06-10) — direct promotion stays illegal:
        (["dep_fail"], "deps_ready"),
    ],
)
def test_illegal_transitions_fail_loud(db, path, bad_event):
    _mk(db, "n1")
    for ev in path:
        g.task_transition(db, "n1", ev)
    before = g.get_task(db, "n1")["state"]
    with pytest.raises(ValueError):
        g.task_transition(db, "n1", bad_event)
    assert g.get_task(db, "n1")["state"] == before  # state untouched on error


def test_unknown_event_fails_loud(db):
    _mk(db, "n1")
    with pytest.raises(ValueError):
        g.task_transition(db, "n1", "bogus_event")


def test_transition_missing_task_fails_loud(db):
    with pytest.raises(ValueError):
        g.task_transition(db, "ghost", "deps_ready")


# ── ready set ──────────────────────────────────────────────────────────────────


def test_recompute_ready_promotes_root_tasks(db):
    _mk(db, "a")
    _mk(db, "b", deps=["a"])
    newly = g.recompute_ready(db, "INBOX")
    assert newly == ["a"]
    assert g.get_task(db, "a")["state"] == "ready"
    assert g.get_task(db, "b")["state"] == "pending"


def test_recompute_ready_requires_all_deps_verified(db):
    _mk(db, "a")
    _mk(db, "b")
    _mk(db, "c", deps=["a", "b"])
    g.recompute_ready(db, "INBOX")
    # verify a only
    for ev in ("claim", "dispatch", "integrate_start", "integrate_ok"):
        g.task_transition(db, "a", ev)
    assert g.recompute_ready(db, "INBOX") == []
    assert g.get_task(db, "c")["state"] == "pending"
    # verify b → c becomes ready
    for ev in ("claim", "dispatch", "integrate_start", "integrate_ok"):
        g.task_transition(db, "b", ev)
    assert g.recompute_ready(db, "INBOX") == ["c"]
    assert g.get_task(db, "c")["state"] == "ready"


def test_recompute_ready_idempotent(db):
    _mk(db, "a")
    assert g.recompute_ready(db, "INBOX") == ["a"]
    assert g.recompute_ready(db, "INBOX") == []


def test_unverified_deps_listing(db):
    _mk(db, "a")
    _mk(db, "b")
    _mk(db, "c", deps=["a", "b"])
    assert sorted(g.unverified_deps(db, "c")) == ["a", "b"]
    g.recompute_ready(db, "INBOX")
    for ev in ("claim", "dispatch", "integrate_start", "integrate_ok"):
        g.task_transition(db, "a", ev)
    assert g.unverified_deps(db, "c") == ["b"]


# ── completion marking ─────────────────────────────────────────────────────────


def test_mark_completion_success_yields_verified(db):
    _mk(db, "n1")
    g.recompute_ready(db, "INBOX")
    state = g.mark_completion(db, "n1", integrate_ok=True, handoff="did the thing")
    assert state == "verified"
    task = g.get_task(db, "n1")
    assert task["handoff"] == "did the thing"
    assert task["verified_at"]


def test_mark_completion_integrate_failure_never_verified(db):
    """REQUIRED PIN (DA B3, 2026-06-10 design): cmd_complete_agent closes the
    thread even when integrate FAILS; task truth must record failed-integration,
    NEVER 'verified'."""
    _mk(db, "n1")
    g.recompute_ready(db, "INBOX")
    state = g.mark_completion(db, "n1", integrate_ok=False)
    assert state == "failed-integration"
    task = g.get_task(db, "n1")
    assert task["state"] != "verified"
    assert task["verified_at"] is None


def test_mark_completion_verify_failure_yields_failed_verify(db):
    _mk(db, "n1")
    g.recompute_ready(db, "INBOX")
    assert g.mark_completion(db, "n1", integrate_ok=True, verify_ok=False) == "failed-verify"


def test_mark_completion_on_terminal_task_fails_loud(db):
    _mk(db, "n1")
    g.recompute_ready(db, "INBOX")
    g.mark_completion(db, "n1", integrate_ok=True)
    with pytest.raises(ValueError):
        g.mark_completion(db, "n1", integrate_ok=True)


# ── reload hygiene (DA round-2, 2026-06-10) ───────────────────────────────────


def test_reload_clears_stale_thread_binding(db):
    """REGRESSION PIN (DA round-2 minor 4, 2026-06-10): 'reload' of a failed
    task kept the dead thread's id on the task — get_task_by_thread and the
    [blocked:]/[ready] context tags kept resolving to a closed thread, and a
    later completion of that zombie thread could re-mark the resurrected task.
    Reload must clear thread_id."""
    _mk(db, "n1")
    g.recompute_ready(db, "INBOX")
    for ev in ("claim", "dispatch", "exec_fail"):
        g.task_transition(db, "n1", ev)
    g.set_task_thread(db, "n1", "dead-thread-uuid")
    assert g.task_transition(db, "n1", "reload") == "pending"
    assert g.get_task(db, "n1")["thread_id"] is None


# ── fan-in race (DA round-2 MAJOR-3, 2026-06-10) ──────────────────────────────


def _verify(db, task_id):
    for ev in ("claim", "dispatch", "integrate_start", "integrate_ok"):
        g.task_transition(db, task_id, ev)


def test_recompute_ready_lost_race_is_noop(db, monkeypatch):
    """REGRESSION PIN (DA round-2 MAJOR-3, 2026-06-10): diamond fan-in — two
    completions recomputed the ready set concurrently; both saw 'd' eligible,
    and the loser's read-then-write task_transition raised ValueError out of
    cmd_complete_agent AFTER the thread had closed (partial side effects).
    The pending→ready promotion must be a CAS; a lost race is a silent no-op."""
    _mk(db, "b")
    _mk(db, "c")
    _mk(db, "d", deps=["b", "c"])
    g.recompute_ready(db, "INBOX")
    _verify(db, "b")
    _verify(db, "c")
    # The loser computed its eligible set BEFORE the winner promoted d:
    monkeypatch.setattr(g, "ready_eligible", lambda *a, **k: ["d"])
    g.task_transition(db, "d", "deps_ready")  # winner promotes first
    assert g.recompute_ready(db, "INBOX") == []  # pre-fix: ValueError
    assert g.get_task(db, "d")["state"] == "ready"


def test_concurrent_recompute_ready_exactly_one_promotes(db, tmp_path):
    """REGRESSION PIN (DA round-2 MAJOR-3, 2026-06-10): two-connection
    concurrent fan-in recompute (claim-race pin pattern) — no crash, task
    promoted exactly once."""
    import threading

    _mk(db, "b")
    _mk(db, "c")
    _mk(db, "d", deps=["b", "c"])
    g.recompute_ready(db, "INBOX")
    _verify(db, "b")
    _verify(db, "c")

    db2 = JuggleDB(db_path=str(tmp_path / "graph.db"))  # separate connection
    barrier = threading.Barrier(2)
    results: list = []
    lock = threading.Lock()

    def _recompute(handle):
        barrier.wait()
        try:
            newly = g.recompute_ready(handle, "INBOX")
        except Exception as e:  # pre-fix: loser raised ValueError
            newly = e
        with lock:
            results.append(newly)

    threads = [threading.Thread(target=_recompute, args=(h,)) for h in (db, db2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not any(isinstance(r, Exception) for r in results), results
    assert sum(r.count("d") for r in results) == 1  # promoted exactly once
    assert g.get_task(db, "d")["state"] == "ready"
