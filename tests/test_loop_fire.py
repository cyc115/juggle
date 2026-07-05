"""Phase-5 pins: CAS-safe watchdog loop firing + failure circuit-breaker +
never-swallow iteration-failure surfacing (loop-entity V1, 2026-07-04).

Pinned invariants:
  * §9 idempotent fire — a due loop instantiates its template exactly once per
    window; a second tick in the same window does NOT re-fire (CAS advanced
    next_run BEFORE instantiate).
  * §Axis-7b crash safety — advancing next_run precedes instantiation, so a
    crash/retry between the two never double-fires.
  * §Axis-2 Objection B overlap policy — a prior iteration still in-flight makes
    this fire SKIP + log; K consecutive skips ESCALATE (never skip-forever).
  * user directive 2026-07-04 (LOAD-BEARING) — ANY iteration failure surfaces
    through the single `surface_iteration_failure` choke point that NEVER
    swallows: (a) a notifications_v2 event carrying the ACTUAL non-empty error,
    (b) a HIGH action item on the loop's thread, (c) an orchestrator-routed
    LOOP_ITERATION_FAILED push. A single failure does all three; the breaker
    pause layers on REPEATED failure.
  * §10b runaway control — N consecutive failed iterations pause the loop +
    escalate; a good iteration resets the consecutive counter.
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
from dbops import event_kinds as ek  # noqa: E402

SESSION = "sess-loop-fire"
PAST = "2020-01-01T00:00:00+00:00"
NOW = "2020-01-01T00:02:00+00:00"  # between PAST and PAST+5m — due exactly once


@pytest.fixture(autouse=True)
def _reset_skips():
    lf.reset_skip_tracker()
    yield
    lf.reset_skip_tracker()


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "loop-fire.db"))
    d.init_db()
    return d


def _template(delivery="deliver", role="researcher"):
    return {"topics": [{
        "id": "digest", "title": "Daily digest", "objective": "obj",
        "delivery": delivery,
        "tasks": [{"id": "t0", "title": "task 0", "prompt": "do the thing",
                   "role": role, "model": "sonnet", "verify_cmd": None, "deps": []}],
    }]}


def _make_loop(db, *, cadence="every 5m", max_cf=3, thread_id="T-loop", next_run=PAST):
    res = create_loop_atomic(db, template=_template(), cadence=cadence,
                             max_consecutive_failures=max_cf)
    loop_id = res["loop_id"]
    # create now binds the loop to its OWN thread; these tests pin a FIXED thread id
    # + a PAST next_run so failures assert against a known thread and the loop is due
    # under a fixed `now`. (The real-thread binding is pinned separately below.)
    with db._connect() as conn:
        conn.execute("UPDATE loops SET thread_id=?, next_run=? WHERE id=?",
                     (thread_id, next_run, loop_id))
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


# ── CAS-safe firing ─────────────────────────────────────────────────────────────
def test_due_loop_fires_once_per_window(db):
    """§9 idempotent fire: a due loop instantiates the template exactly once; a
    second tick in the SAME window does not re-fire (CAS advanced next_run)."""
    loop_id, res = _make_loop(db)
    pid = res["project_id"]
    _set_iter_state(db, pid, loop_id, 0, "verified")  # prior iteration succeeded

    lf.fire_due_loops(db, SESSION, now=NOW)
    assert _iter_task_count(db, pid, loop_id, 1) == 1  # r1 fired
    assert db.get_loop(loop_id)["run_seq"] == 1

    lf.fire_due_loops(db, SESSION, now=NOW)  # next_run now PAST+5m > NOW → not due
    assert _iter_task_count(db, pid, loop_id, 2) == 0
    assert db.get_loop(loop_id)["run_seq"] == 1


def test_crash_between_fire_and_advance_no_double_fire(db):
    """§Axis-7b: next_run CAS precedes instantiate, so a retry (the crash-recovery
    tick) in the same window produces exactly ONE iteration — never a duplicate."""
    loop_id, res = _make_loop(db)
    pid = res["project_id"]
    _set_iter_state(db, pid, loop_id, 0, "verified")

    lf.fire_due_loops(db, SESSION, now=NOW)   # fires r1, advances next_run
    lf.fire_due_loops(db, SESSION, now=NOW)   # "retry after crash" — CAS-gated no-op

    assert _iter_task_count(db, pid, loop_id, 1) == 1
    assert _iter_task_count(db, pid, loop_id, 2) == 0
    assert db.get_loop(loop_id)["run_seq"] == 1  # advanced exactly once


def test_cas_advance_loses_race_skips(db):
    """CAS correctness (cas_state precedent): once next_run is advanced off the
    expected value, a second advance with the same expected loses (rowcount 0)."""
    loop_id, _ = _make_loop(db)
    assert db.advance_next_run_cas(loop_id, expected=PAST, new="2020-01-01T00:05:00+00:00") == 1
    assert db.advance_next_run_cas(loop_id, expected=PAST, new="2020-01-01T00:10:00+00:00") == 0
    assert db.get_loop(loop_id)["next_run"] == "2020-01-01T00:05:00+00:00"


# ── Overlap policy ──────────────────────────────────────────────────────────────
def test_overlap_skip_when_prior_iteration_in_flight(db):
    """§9 overlap: prior iteration un-terminal → this fire is SKIPPED (no new
    iteration instantiated) and logged."""
    loop_id, res = _make_loop(db)
    pid = res["project_id"]
    _set_iter_state(db, pid, loop_id, 0, "running")  # prior still in flight

    lf.fire_due_loops(db, SESSION, now=NOW)
    assert _iter_task_count(db, pid, loop_id, 1) == 0  # nothing fired
    assert db.get_loop(loop_id)["run_seq"] == 0


def test_overlap_skip_escalates_after_k(db):
    """§Axis-2 Objection B: K consecutive overlap-skips ESCALATE (orchestrator
    push) — a wedged iteration surfaces, never silently killing the loop."""
    loop_id, res = _make_loop(db)
    pid = res["project_id"]
    _set_iter_state(db, pid, loop_id, 0, "running")  # permanently in flight

    k = lf.max_skips()
    for _ in range(k):
        # re-arm next_run to PAST each window so it stays due and keeps skipping
        with db._connect() as conn:
            conn.execute("UPDATE loops SET next_run=? WHERE id=?", (PAST, loop_id))
            conn.commit()
        lf.fire_due_loops(db, SESSION, now=NOW)

    pushed = [n for n in db.get_notifications_for_session(SESSION)
              if n["message"] and "wedged" in n["message"].lower()]
    assert pushed, "K overlap-skips must emit an escalation event"
    assert _iter_task_count(db, pid, loop_id, 1) == 0  # still never fired


# ── Loop owns a real thread; failures surface ON it (not the orphan bucket) ─────
def test_created_loop_has_thread_and_failure_item_lands_on_it(db):
    """Review gap (2026-07-04): create_loop_atomic inserted thread_id=NULL, so a
    failing iteration's HIGH action item landed in the null-thread ORPHAN bucket
    instead of on the loop's OWN thread. A loop created via the REAL create path
    must own a real thread, and its failure item must land ON that thread (the user
    requirement: loop failures surface in the ACTION PANE on the loop)."""
    res = create_loop_atomic(db, template=_template(), cadence="every 5m")
    loop_id = res["loop_id"]
    thread_id = db.get_loop(loop_id)["thread_id"]
    assert thread_id, "created loop must own a real thread (not NULL/orphan)"
    assert res["thread_id"] == thread_id  # create returns the bound thread

    pid = res["project_id"]
    _set_iter_state(db, pid, loop_id, 0, "failed-verify")  # prior iteration failed
    with db._connect() as conn:  # make it due — timer only; thread untouched
        conn.execute("UPDATE loops SET next_run=? WHERE id=?", (PAST, loop_id))
        conn.commit()

    lf.fire_due_loops(db, SESSION, now=NOW)

    items = db.get_open_action_items()
    on_thread = [i for i in items if i["thread_id"] == thread_id]
    assert on_thread, "failure HIGH action item must land on the loop's thread"
    assert on_thread[0]["priority"] == "high"
    assert not [i for i in items if i["thread_id"] is None], \
        "no failure item may land in the null-thread ORPHAN bucket"


# ── Never-swallow failure surfacing (LOAD-BEARING) ──────────────────────────────
def test_single_iteration_failure_surfaces_all_three_never_swallowed(db):
    """User directive 2026-07-04: a SINGLE failed iteration surfaces ALL THREE and
    swallows NONE — (a) notifications_v2 event with the ACTUAL non-empty error,
    (b) HIGH action item on the loop's thread, (c) orchestrator-routed
    LOOP_ITERATION_FAILED push."""
    loop_id, res = _make_loop(db, thread_id="T-fail")
    pid = res["project_id"]
    _set_iter_state(db, pid, loop_id, 0, "failed-verify")  # prior iteration failed

    lf.fire_due_loops(db, SESSION, now=NOW)

    # (a)+(c): a notifications_v2 LOOP_ITERATION_FAILED event, orchestrator-routed,
    # carrying non-empty error text naming the failed node/state.
    notifs = db.get_notifications_for_session(SESSION)
    failed_ev = [n for n in notifs if "failed-verify" in (n["message"] or "")]
    assert failed_ev, "iteration failure must emit a notification carrying the error"
    ev = failed_ev[0]
    assert ev["message"].strip(), "error text must be NON-EMPTY (never swallowed)"
    assert ek.handled_by_for_kind(ek.LOOP_ITERATION_FAILED) == "orchestrator"
    with db._connect() as conn:
        row = conn.execute(
            "SELECT kind, handled_by FROM notifications_v2 WHERE id=?", (ev["id"],)
        ).fetchone()
    assert row[0] == ek.LOOP_ITERATION_FAILED
    assert row[1] == "orchestrator"  # pushed to the orchestrator, not DB-row-only

    # (b): a HIGH action item on the loop's thread.
    items = [i for i in db.get_open_action_items() if i["thread_id"] == "T-fail"]
    assert items, "a HIGH action item must be filed on the loop's thread"
    assert items[0]["priority"] == "high"
    assert items[0]["message"].strip()


def test_repeated_failure_action_item_deduped(db):
    """Repeated failure stays VISIBLE without flooding: N failing iterations file
    ONE open HIGH action item (add_action_item_once dedup) while STILL re-pushing
    the orchestrator each time."""
    loop_id, res = _make_loop(db, thread_id="T-dedup", max_cf=99)
    pid = res["project_id"]

    _set_iter_state(db, pid, loop_id, 0, "failed-verify")
    lf.fire_due_loops(db, SESSION, now=NOW)  # cycle 1 — fires r1
    _set_iter_state(db, pid, loop_id, 1, "failed-verify")
    with db._connect() as conn:
        conn.execute("UPDATE loops SET next_run=? WHERE id=?", (PAST, loop_id))
        conn.commit()
    lf.fire_due_loops(db, SESSION, now=NOW)  # cycle 2 — sees r1 failed

    items = [i for i in db.get_open_action_items() if i["thread_id"] == "T-dedup"]
    assert len(items) == 1, "repeated failure must not stack duplicate HIGH items"
    notifs = [n for n in db.get_notifications_for_session(SESSION)
              if "failed-verify" in (n["message"] or "")]
    assert len(notifs) >= 2, "each failure still re-pushes the orchestrator"


# ── Circuit breaker ─────────────────────────────────────────────────────────────
def test_circuit_breaker_pauses_after_n_failures(db):
    """§10b runaway control: N=2 consecutive failed iterations pause the loop +
    escalate (layered ON TOP of the per-failure surfacing)."""
    loop_id, res = _make_loop(db, thread_id="T-brk", max_cf=2)
    pid = res["project_id"]

    _set_iter_state(db, pid, loop_id, 0, "failed-verify")
    lf.fire_due_loops(db, SESSION, now=NOW)  # failure #1 → bump to 1, fires r1
    assert db.get_loop(loop_id)["status"] == "active"

    _set_iter_state(db, pid, loop_id, 1, "failed-verify")
    with db._connect() as conn:
        conn.execute("UPDATE loops SET next_run=? WHERE id=?", (PAST, loop_id))
        conn.commit()
    lf.fire_due_loops(db, SESSION, now=NOW)  # failure #2 → trips breaker

    loop = db.get_loop(loop_id)
    assert loop["status"] == "paused"
    assert loop["consecutive_failures"] >= 2
    paused_ev = [n for n in db.get_notifications_for_session(SESSION)
                 if "pause" in (n["message"] or "").lower()]
    assert paused_ev, "breaker trip must escalate a LOOP_PAUSED event"


def test_successful_iteration_resets_failure_counter(db):
    """Breaker trips on CONSECUTIVE failures only: a good iteration resets the
    consecutive-failure counter."""
    loop_id, res = _make_loop(db, thread_id="T-reset", max_cf=3)
    pid = res["project_id"]

    _set_iter_state(db, pid, loop_id, 0, "failed-verify")
    lf.fire_due_loops(db, SESSION, now=NOW)  # failure #1 → counter 1, fires r1
    assert db.get_loop(loop_id)["consecutive_failures"] == 1

    _set_iter_state(db, pid, loop_id, 1, "verified")  # r1 succeeds
    with db._connect() as conn:
        conn.execute("UPDATE loops SET next_run=? WHERE id=?", (PAST, loop_id))
        conn.commit()
    lf.fire_due_loops(db, SESSION, now=NOW)  # success → resets

    assert db.get_loop(loop_id)["consecutive_failures"] == 0


def test_failed_instantiate_rolls_back_seq_bump_and_surfaces(db, monkeypatch):
    """code-review #2 + #1 (2026-07-04): a failed instantiation rolls the run_seq
    bump back (NO phantom empty iteration — an empty seq would read as a false
    'success' next window, silently resetting the breaker + masking the failure)
    AND the machinery error is surfaced through the never-swallow choke point, never
    reduced to a log line."""
    loop_id, res = _make_loop(db, thread_id="T-mach")
    pid = res["project_id"]
    _set_iter_state(db, pid, loop_id, 0, "verified")  # prior success → would fire r1

    def _boom(*a, **k):
        raise RuntimeError("injected instantiate failure")
    import juggle_loop_regen as regen
    monkeypatch.setattr(regen, "instantiate_generation", _boom)

    lf.fire_due_loops(db, SESSION, now=NOW)

    loop = db.get_loop(loop_id)
    assert loop["run_seq"] == 0, "seq bump must roll back with the failed instantiate"
    assert _iter_task_count(db, pid, loop_id, 1) == 0, "no phantom r1 nodes"
    notifs = [n for n in db.get_notifications_for_session(SESSION)
              if "machinery error" in (n["message"] or "")]
    assert notifs, "machinery error must push the orchestrator (never swallowed)"
    items = [i for i in db.get_open_action_items() if i["thread_id"] == "T-mach"]
    assert items and items[0]["priority"] == "high"


def test_paused_loop_does_not_fire(db):
    """A paused loop is inert: fire_due_loops skips it entirely (list_active_loops
    excludes non-active)."""
    loop_id, res = _make_loop(db)
    pid = res["project_id"]
    _set_iter_state(db, pid, loop_id, 0, "verified")
    db.set_loop_status(loop_id, "paused")

    lf.fire_due_loops(db, SESSION, now=NOW)
    assert _iter_task_count(db, pid, loop_id, 1) == 0
    assert db.get_loop(loop_id)["run_seq"] == 0


def test_no_due_loops_is_a_noop(db):
    """Regression pin: a tick with NO due loop makes zero writes — a not-yet-due
    active loop is left untouched (preserves non-loop tick behavior)."""
    loop_id, res = _make_loop(db, next_run="2999-01-01T00:00:00+00:00")
    pid = res["project_id"]
    _set_iter_state(db, pid, loop_id, 0, "verified")

    lf.fire_due_loops(db, SESSION, now=NOW)
    assert _iter_task_count(db, pid, loop_id, 1) == 0
    assert db.get_loop(loop_id)["run_seq"] == 0
    assert db.get_notifications_for_session(SESSION) == []
