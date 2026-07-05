"""P3a pins — loop identity: stable topic + atomic reopen-regenerate + integrate-
state reset (loop-entity V2 §0b, 2026-07-04).

A loop is a long-lived container: ONE stable kind='topic' node, per-fire task
generations attached via ``reopen``. Each fire drives ``reopen`` on the stable
topic AND instantiates the new ``-r<seq>-`` generation in ONE transaction, and the
reopen clears the topic's durable integrate state (reopen PRESERVES it,
db_topics_marking.py:84) so generation N never poisons N+1.

Pinned invariants:
  * a stable topic integrates TWO generations with TWO shas (the reopen clears the
    prior generation's merged_sha, so gen N+1 verifies against its OWN sha — not
    gen N's stale, still-ancestor sha);
  * the FOUR integrate seams reset on reopen — a gen-N repair that consumed the
    BACKSTOP_TOTAL_PER_TOPIC budget (or left a pending sha) does NOT block gen N+1
    (the happy-path new-sha pin does NOT catch this);
  * reopen+regenerate is ATOMIC — a fire that reopens then fails instantiation
    rolls the reopen back to the prior terminal (never wedged in 'open'), and the
    next fire re-fires cleanly;
  * iteration_outcome keys on the run-namespaced task ids (a stable topic does not
    leak one generation's outcome into another).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from juggle_cmd_loop_create import create_loop_atomic  # noqa: E402
from dbops import db_topics as t  # noqa: E402
import juggle_loop_fire as lf  # noqa: E402

SESSION = "sess-loop-identity"
PAST = "2020-01-01T00:00:00+00:00"
NOW = "2020-01-01T00:02:00+00:00"  # between PAST and PAST+5m — due exactly once
SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture(autouse=True)
def _reset_skips():
    lf.reset_skip_tracker()
    yield
    lf.reset_skip_tracker()


@pytest.fixture(autouse=True)
def _bypass_merge_gate(monkeypatch):
    """Drive topics to 'verified' without a real git merge — this suite pins the
    loop reopen/reset behaviour, not the merged_sha gate (covered elsewhere)."""
    monkeypatch.setattr("dbops.db_topics._verified_allowed", lambda *a, **k: True)


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "loop-identity.db"))
    d.init_db()
    return d


def _merge_template():
    return {"topics": [{
        "id": "digest", "title": "Daily digest", "objective": "obj",
        "delivery": "merge",
        "tasks": [{"id": "t0", "title": "task 0", "prompt": "do the thing",
                   "role": "coder", "model": "sonnet", "verify_cmd": None, "deps": []}],
    }]}


def _make_loop(db):
    res = create_loop_atomic(db, template=_merge_template(), cadence="every 5m")
    loop_id = res["loop_id"]
    with db._connect() as conn:
        conn.execute("UPDATE loops SET next_run=? WHERE id=?", (PAST, loop_id))
        conn.commit()
    return loop_id, res


def _rearm(db, loop_id):
    with db._connect() as conn:
        conn.execute("UPDATE loops SET next_run=? WHERE id=?", (PAST, loop_id))
        conn.commit()


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


def _integrate_generation(db, project_id, loop_id, stable_topic_id, seq, sha):
    """Simulate the integrate pipeline completing generation ``seq``: its tasks pass
    and the stable topic merges to ``sha`` and verifies."""
    _set_iter_state(db, project_id, loop_id, seq, "verified")
    t.set_topic_merged_sha(db, stable_topic_id, sha)
    t.mark_topic_completion(db, stable_topic_id, integrate_ok=True)  # -> verified


# ── A stable topic integrates two generations with two shas ─────────────────────
def test_merge_loop_fires_twice_integrates_two_shas(db):
    """§0b: ONE stable topic hosts TWO integrate generations with TWO shas. The fire
    reopens the SAME topic (not a fresh one) and clears gen-N's merged_sha, so gen
    N+1 verifies against its OWN sha rather than gen N's stale (still-ancestor) sha.
    Incident 2026-07-04: reopen PRESERVES merged_sha (db_topics_marking.py:84)."""
    loop_id, res = _make_loop(db)
    pid, stable = res["project_id"], res["topic_id"]

    # gen r0 runs + integrates with SHA_A
    _integrate_generation(db, pid, loop_id, stable, 0, SHA_A)
    assert t.get_topic(db, stable)["merged_sha"] == SHA_A

    # fire → the SAME stable topic reopens; its gen-r0 integrate state is cleared
    lf.fire_due_loops(db, SESSION, now=NOW)
    reopened = t.get_topic(db, stable)
    assert reopened["state"] == "open", "the stable topic reopens (not a fresh topic)"
    assert reopened["merged_sha"] is None, "gen-r0 merged_sha must be cleared on reopen"
    assert db.get_loop(loop_id)["run_seq"] == 1
    assert _iter_task_count(db, pid, loop_id, 1) == 1  # r1 generation instantiated

    # gen r1 runs + integrates with a DIFFERENT sha
    _integrate_generation(db, pid, loop_id, stable, 1, SHA_B)
    final = t.get_topic(db, stable)
    assert final["state"] == "verified"
    assert final["merged_sha"] == SHA_B
    assert SHA_A != final["merged_sha"], "two generations integrated two distinct shas"


# ── THE four-seams pin: repair/pending carryover must not block gen N+1 ──────────
def test_repair_carryover_does_not_block_next_generation(db):
    """§0b consequence 2 (FOUR seams): a gen-N generation that CONSUMED its repair
    budget (fail_envelope.attempts_total == BACKSTOP_TOTAL_PER_TOPIC) or left a
    pending sha must NOT block gen N+1 — reopen clears fail_envelope, pending
    sha/repo, submitted_rev, merged_sha, and the reintegrate backoff. The happy-path
    new-sha pin does NOT catch this. Incident 2026-07-04: reopen preserves ALL of
    it, so the 3-repairs-per-topic backstop would refuse N+1's integrate."""
    from juggle_integrate_envelope import (
        BACKSTOP_TOTAL_PER_TOPIC, _prior_envelope, check_retry_policy,
    )

    loop_id, res = _make_loop(db)
    pid, stable = res["project_id"], res["topic_id"]
    _integrate_generation(db, pid, loop_id, stable, 0, SHA_A)

    # gen r0 exhausted its repair budget AND left a stale pending sha + submitted_rev
    poison = json.dumps({
        "attempts_total": BACKSTOP_TOTAL_PER_TOPIC,
        "attempts_by_signature": {"sig-abc": 1},
    })
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET fail_envelope=?, pending_merged_sha=?, "
            "pending_merged_repo=?, submitted_rev=?, reintegrate_attempts=? "
            "WHERE id=? AND kind='topic'",
            (poison, "stale" + "0" * 35, "/some/repo", "cyc_stale", 3, stable),
        )
        conn.commit()

    lf.fire_due_loops(db, SESSION, now=NOW)  # reopen must reset ALL four seams

    topic = t.get_topic(db, stable)
    assert topic["fail_envelope"] is None, "fail_envelope (repair budget) must reset"
    assert topic["pending_merged_sha"] is None, "pending_merged_sha must reset"
    assert topic["pending_merged_repo"] is None, "pending_merged_repo must reset"
    assert topic["submitted_rev"] is None, "submitted_rev audit must reset"
    assert topic["merged_sha"] is None, "merged_sha must reset"
    with db._connect() as conn:
        ra = conn.execute(
            "SELECT reintegrate_attempts FROM nodes WHERE id=?", (stable,)
        ).fetchone()[0]
    assert (ra or 0) == 0, "reintegrate backoff must reset (db_reintegrate.forget)"

    # behavioural proof: gen N+1's repair is NOT refused by the stale backstop
    decision = check_retry_policy(
        _prior_envelope(t.get_topic(db, stable)), "sig-abc",
        trunk_at_attempt=None, head_at_attempt=None,
        current_trunk="x", current_head="y",
    )
    assert decision.allowed, "gen-N repair budget must not block gen-N+1's integrate"


# ── Atomic reopen+regenerate: a mid-fire failure rolls back to the terminal ──────
def test_fire_fails_after_reopen_leaves_topic_terminal(db, monkeypatch):
    """§0b consequence 1: reopen + regenerate is ATOMIC. A fire that reopens the
    stable topic then FAILS instantiation must roll the reopen back to the prior
    terminal — never wedge it in 'open' (open has no reopen edge and kind='topic'
    never auto-terminalizes). The next fire then re-fires cleanly. Incident
    2026-07-04: a non-atomic reopen leaves the topic permanently in 'open'."""
    import juggle_loop_regen as regen

    loop_id, res = _make_loop(db)
    pid, stable = res["project_id"], res["topic_id"]
    _integrate_generation(db, pid, loop_id, stable, 0, SHA_A)
    assert t.get_topic(db, stable)["state"] == "verified"

    calls = {"n": 0}
    real = regen.instantiate_generation

    def _boom_once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected instantiate failure")
        return real(*a, **k)

    monkeypatch.setattr(regen, "instantiate_generation", _boom_once)

    lf.fire_due_loops(db, SESSION, now=NOW)  # reopen-then-fail

    topic = t.get_topic(db, stable)
    assert topic["state"] == "verified", \
        "reopen must roll back on instantiate failure (not wedge in 'open')"
    assert db.get_loop(loop_id)["run_seq"] == 0, "run_seq bump rolled back too"
    assert _iter_task_count(db, pid, loop_id, 1) == 0, "no phantom r1 nodes"

    # next fire re-fires cleanly (the topic was still a fire-able terminal)
    _rearm(db, loop_id)
    lf.fire_due_loops(db, SESSION, now=NOW)
    assert db.get_loop(loop_id)["run_seq"] == 1
    assert t.get_topic(db, stable)["state"] == "open"  # reopened + regenerated
    assert _iter_task_count(db, pid, loop_id, 1) == 1


# ── iteration_outcome keys on the run-namespaced generation (unaffected) ─────────
def test_iteration_outcome_keys_on_generation(db):
    """§0b: the stable topic does not break iteration_outcome — it keys on the
    run-namespaced task ids ({loop}-r{seq}-%), so each generation's outcome is
    independent and one generation never leaks into another's classification."""
    loop_id, res = _make_loop(db)
    pid, stable = res["project_id"], res["topic_id"]

    _integrate_generation(db, pid, loop_id, stable, 0, SHA_A)
    assert lf.iteration_outcome(db, pid, loop_id, 0) == ("success", "")
    # a generation with no tasks yet reads success (nothing to wait on)
    assert lf.iteration_outcome(db, pid, loop_id, 1)[0] == "success"

    lf.fire_due_loops(db, SESSION, now=NOW)  # instantiate r1 (fresh 'open' tasks)

    # r0 stays 'success'; r1 is now 'in_flight' — keyed strictly per generation
    assert lf.iteration_outcome(db, pid, loop_id, 0) == ("success", "")
    assert lf.iteration_outcome(db, pid, loop_id, 1)[0] == "in_flight"
