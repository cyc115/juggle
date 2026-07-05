"""P5b loop-slot budgeting — disjoint loop pool (loop-entity V2 §8, 2026-07-05).

A loop must NOT share the interactive ``JUGGLE_MAX_THREADS`` pool: the user expects
dozens of standing loops, so loops get their OWN disjoint budget
``JUGGLE_MAX_LOOP_THREADS`` (default 50). The fire step refuses / overlap-skips a due
loop when the LOOP pool is at cap; firing concurrency stays gated by
``MAX_BACKGROUND_AGENTS`` (unchanged). Loop firing instantiates task nodes under the
loop's stable topic and reuses its bound thread — it never allocates an interactive
conversation-thread slot, so the two pools are genuinely disjoint.

Pinned invariants (plan P5b):
  * due-loop-skipped-when-loop-pool-at-cap — a due loop whose prior iteration is
    terminal does NOT start a new iteration when the loop pool is at cap; it fires
    again once the pool has headroom.
  * loops-never-consume-interactive-MAX_THREADS-slots — the loop gate keys on
    ``MAX_LOOP_THREADS`` only: interactive spawn headroom never rescues a
    loop-capped fire, and an exhausted interactive pool never blocks a loop that
    still has loop-pool headroom.
  * interactive-threads-unaffected-by-loop-count — a loop pool AT its cap does not
    reduce interactive ``create_thread`` capacity (still bounded by ``MAX_THREADS``
    over conversation nodes, disjoint from the loop pool).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from juggle_cmd_loop_create import create_loop_atomic  # noqa: E402
import juggle_loop_fire as lf  # noqa: E402

SESSION = "sess-loop-budget"
PAST = "2020-01-01T00:00:00+00:00"
FUTURE = "2020-01-01T09:00:00+00:00"
NOW = "2020-01-01T00:02:00+00:00"  # between PAST and PAST+5m — due exactly once


@pytest.fixture(autouse=True)
def _reset_skips():
    lf.reset_skip_tracker()
    yield
    lf.reset_skip_tracker()


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "loop-budget.db"))
    d.init_db()
    return d


def _template():
    return {"topics": [{
        "id": "digest", "title": "Daily digest", "objective": "obj",
        "delivery": "deliver",
        "tasks": [{"id": "t0", "title": "task 0", "prompt": "do the thing",
                   "role": "researcher", "model": "sonnet",
                   "verify_cmd": None, "deps": []}],
    }]}


def _make_loop(db, *, next_run=PAST):
    """Create a real loop (own project + bound thread + r0 generation) and set its
    next_run so due-ness is controllable under a fixed `now`."""
    res = create_loop_atomic(db, template=_template(), cadence="every 5m")
    loop_id = res["loop_id"]
    with db._connect() as conn:
        conn.execute("UPDATE loops SET next_run=? WHERE id=?", (next_run, loop_id))
        conn.commit()
    return loop_id, res


def _set_iter_state(db, project_id, loop_id, seq, state):
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET state=? WHERE kind='task' AND project_id=? AND id LIKE ?",
            (state, project_id, f"{loop_id}-r{seq}-%"),
        )
        conn.commit()


def _iter_task_count(db, project_id, loop_id, seq):
    with db._connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind='task' AND project_id=? AND id LIKE ?",
            (project_id, f"{loop_id}-r{seq}-%"),
        ).fetchone()[0]


def _rearm(db, loop_id, next_run=PAST):
    with db._connect() as conn:
        conn.execute("UPDATE loops SET next_run=? WHERE id=?", (next_run, loop_id))
        conn.commit()


def _conv_count(db):
    with db._connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind='conversation' AND state != 'archived'"
        ).fetchone()[0]


def _actions(results, loop_id):
    return [a for lid, a in results if lid == loop_id]


# ── due-loop-skipped-when-loop-pool-at-cap ──────────────────────────────────────
def test_due_loop_skipped_when_loop_pool_at_cap(db, monkeypatch):
    """P5b (2026-07-05): a due loop must NOT start a new iteration while the disjoint
    loop pool is at cap — pre-fix the fire step had no loop budget and fired
    unconditionally. Once the pool has headroom the same loop fires."""
    # A occupies the one loop-pool slot: r0 in-flight, and NOT due so it just sits live.
    a_id, a_res = _make_loop(db, next_run=FUTURE)
    _set_iter_state(db, a_res["project_id"], a_id, 0, "running")  # in-flight → occupies

    # B is due with a TERMINAL prior iteration → wants to fire its next generation.
    b_id, b_res = _make_loop(db, next_run=PAST)
    _set_iter_state(db, b_res["project_id"], b_id, 0, "verified")  # prior done

    monkeypatch.setattr(lf, "MAX_LOOP_THREADS", 1)  # pool cap = 1, A holds it
    assert lf.count_live_loop_pool(db) == 1

    results = lf.fire_due_loops(db, SESSION, now=NOW)
    assert _actions(results, b_id) == ["pool_full"]           # refused, not fired
    assert _iter_task_count(db, b_res["project_id"], b_id, 1) == 0
    assert db.get_loop(b_id)["run_seq"] == 0                  # no generation started

    # Headroom → the same due loop fires (re-arm next_run: the pool_full attempt
    # already advanced it via CAS, same as an overlap-skip).
    monkeypatch.setattr(lf, "MAX_LOOP_THREADS", 2)
    _rearm(db, b_id, PAST)
    results = lf.fire_due_loops(db, SESSION, now=NOW)
    assert _actions(results, b_id) == ["fired"]
    assert _iter_task_count(db, b_res["project_id"], b_id, 1) == 1
    assert db.get_loop(b_id)["run_seq"] == 1


# ── loops-never-consume-interactive-MAX_THREADS-slots ───────────────────────────
def test_loops_never_consume_interactive_MAX_THREADS_slots(db, monkeypatch):
    """P5b (2026-07-05): the loop gate keys on MAX_LOOP_THREADS, never the interactive
    MAX_THREADS pool. Interactive spawn headroom must NOT rescue a loop-capped fire
    (RED pre-fix: no gate → fired), and an exhausted interactive pool must NOT block a
    loop that still has loop-pool headroom."""
    import dbops.threads as _threads
    import dbops.schema as _schema

    # (A) loop pool AT cap while interactive pool is WIDE OPEN → still refused.
    a_id, a_res = _make_loop(db, next_run=FUTURE)
    _set_iter_state(db, a_res["project_id"], a_id, 0, "running")   # holds the slot
    b_id, b_res = _make_loop(db, next_run=PAST)
    _set_iter_state(db, b_res["project_id"], b_id, 0, "verified")  # prior done
    monkeypatch.setattr(lf, "MAX_LOOP_THREADS", 1)                 # loop pool full
    # interactive pool left at the generous test default (MAX_THREADS=10) — headroom.
    results = lf.fire_due_loops(db, SESSION, now=NOW)
    assert _actions(results, b_id) == ["pool_full"], \
        "interactive headroom must not rescue a loop-pool-capped fire"

    # (B) interactive pool EXHAUSTED while the loop pool has headroom → loop fires.
    monkeypatch.setattr(lf, "MAX_LOOP_THREADS", 50)
    monkeypatch.setattr(_threads, "MAX_THREADS", _conv_count(db))  # interactive full
    monkeypatch.setattr(_schema, "MAX_THREADS", _conv_count(db))
    before = _conv_count(db)
    _rearm(db, b_id, PAST)
    results = lf.fire_due_loops(db, SESSION, now=NOW)
    assert _actions(results, b_id) == ["fired"], \
        "an exhausted interactive pool must not block a loop with loop-headroom"
    assert _iter_task_count(db, b_res["project_id"], b_id, 1) == 1
    assert _conv_count(db) == before, \
        "firing a loop iteration must allocate ZERO interactive conversation slots"


# ── interactive-threads-unaffected-by-loop-count ────────────────────────────────
def test_interactive_threads_unaffected_by_loop_count(db, monkeypatch):
    """P5b (2026-07-05): a loop pool AT its cap must NOT reduce interactive
    create_thread capacity — the two budgets are disjoint. Interactive threads stay
    bounded by MAX_THREADS over conversation nodes, never by the loop pool."""
    # Fill the loop pool to its cap with live loops (each holds its own bound thread).
    loops = []
    for _ in range(3):
        lid, res = _make_loop(db, next_run=FUTURE)
        _set_iter_state(db, res["project_id"], lid, 0, "running")  # in-flight → live
        loops.append(lid)
    monkeypatch.setattr(lf, "MAX_LOOP_THREADS", len(loops))        # loop pool AT cap
    assert lf.count_live_loop_pool(db) == len(loops)              # cap reached

    import dbops.threads as _threads
    # Interactive capacity is MAX_THREADS over conversation nodes, independent of the
    # loop pool being full: we can still create interactive threads up to that bound.
    room = _threads.MAX_THREADS - _conv_count(db)
    assert room >= 1, "test env should leave interactive headroom"
    made = [db.create_thread(f"interactive {i}", SESSION) for i in range(room)]
    assert len(made) == room, \
        "a full loop pool must not block interactive thread creation"
    # And the loop pool is STILL exactly at its cap — interactive threads didn't leak
    # into the loop budget either.
    assert lf.count_live_loop_pool(db) == len(loops)
