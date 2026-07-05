"""P3b pins — loop-progress rollups scope to the LIVE generation + cancelled
reconcile (loop-entity V2 §7, 2026-07-05).

A loop's ONE stable topic accumulates a new run-namespaced TASK generation
(``<loop_id>-r<seq>-<task>``) per fire, so a naive rollup over ALL of the topic's
task children reads ``168/170`` noise. Every loop-progress read must scope to the
CURRENT run_seq generation (the same ``<loop_id>-r<seq>-%`` key iteration_outcome
already uses) so the loop reads ``2/3`` regardless of prior-generation
accumulation. This change also reconciles the pre-existing ``cancelled``
disagreement — the executed graph_dag SQL counted ``cancelled`` as done while
``counts_from_states`` did not — aligning both on ``COMPLETION_TERMINAL_STATES``.

Pinned invariants (RED on the pre-P3b code):
  * the executed rollup (``_load_one``) AND ``graph_counts``/``counts_from_states``
    read ONLY the live generation (``2/3``, not ``5/6``) — RED today: every
    generation is counted;
  * the learnings-rollup COUNT (``_task_state_counts``) scopes to the live
    generation too;
  * a pure-``deliver`` single-generation loop still reads N/N (``delivered`` counts
    as done — scoping must not under-count it) — already holds, pinned as a guard;
  * both rollups agree that ``cancelled`` counts as done — RED today:
    ``counts_from_states`` excluded ``cancelled``.
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
import juggle_graph_status as gs  # noqa: E402
from juggle_cockpit_graph_dag import _load_one  # noqa: E402
from juggle_learnings_rollup import _task_state_counts  # noqa: E402

SESSION = "sess-loop-gc"
PAST = "2020-01-01T00:00:00+00:00"
NOW = "2020-01-01T00:02:00+00:00"  # between PAST and PAST+5m — due exactly once


@pytest.fixture(autouse=True)
def _reset_skips():
    lf.reset_skip_tracker()
    yield
    lf.reset_skip_tracker()


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "loop-gc.db"))
    d.init_db()
    return d


def _template(delivery: str, n_tasks: int = 3):
    return {"topics": [{
        "id": "digest", "title": "Daily digest", "objective": "obj",
        "delivery": delivery,
        "tasks": [
            {"id": f"t{i}", "title": f"task {i}", "prompt": "do the thing",
             "role": "coder", "model": "sonnet", "verify_cmd": None, "deps": []}
            for i in range(n_tasks)
        ],
    }]}


def _make_loop(db, delivery="merge", n_tasks=3):
    res = create_loop_atomic(db, template=_template(delivery, n_tasks),
                             cadence="every 5m")
    with db._connect() as conn:
        conn.execute("UPDATE loops SET next_run=? WHERE id=?", (PAST, res["loop_id"]))
        conn.commit()
    return res["loop_id"], res


def _set_gen_states(db, pid, loop_id, seq, state, *, only=None):
    """Force the states of generation ``seq``'s task nodes (test seam). ``only`` =
    an iterable of base task ids (t0/t1/…) to restrict to; None = the whole gen."""
    with db._connect() as conn:
        if only is None:
            conn.execute(
                "UPDATE nodes SET state=? WHERE kind='task' AND project_id=? AND id LIKE ?",
                (state, pid, f"{loop_id}-r{seq}-%"),
            )
        else:
            for base in only:
                conn.execute(
                    "UPDATE nodes SET state=? WHERE kind='task' AND id=?",
                    (state, f"{loop_id}-r{seq}-{base}"),
                )
        conn.commit()


def _mark_topic_terminal(db, stable, state="verified"):
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET state=? WHERE id=? AND kind='topic'", (state, stable)
        )
        conn.commit()


def _fire_to_r1(db):
    """Create a 3-task merge loop, integrate gen r0, fire → gen r1 (fresh 'open'),
    then set 2 of r1's 3 tasks 'verified'. The stable topic now hosts BOTH r0 (3
    verified) and r1 (2 verified + 1 open) — a naive rollup reads 5/6; a
    generation-scoped rollup reads 2/3."""
    loop_id, res = _make_loop(db, delivery="merge", n_tasks=3)
    pid, stable = res["project_id"], res["topic_id"]
    _set_gen_states(db, pid, loop_id, 0, "verified")  # gen r0 all done
    _mark_topic_terminal(db, stable, "verified")      # fire-ready reopenable terminal

    lf.fire_due_loops(db, SESSION, now=NOW)            # -> run_seq=1, gen r1 'open'
    assert db.get_loop(loop_id)["run_seq"] == 1
    _set_gen_states(db, pid, loop_id, 1, "verified", only=("t0", "t1"))  # 2/3 done
    return loop_id, pid, stable


def _loop_topic(conn, pid, stable):
    dag = _load_one(conn, pid)
    return next(t for t in dag.tasks if t.id == stable)


# ── the executed rollup + graph_counts read the LIVE generation only ────────────
def test_loop_reads_live_generation_only(db):
    """§7.2 (RED today): the loop's stable topic hosts an accumulating generation
    per fire, so BOTH the executed rollup (_load_one per-topic count) and
    graph_counts/counts_from_states must scope to the current run_seq generation —
    the loop reads 2/3, not 5/6. Incident 2026-07-05: unscoped rollups aggregate
    every generation (the 168/170 noise)."""
    loop_id, pid, stable = _fire_to_r1(db)

    # executed rollup (juggle_cockpit_graph_dag._load_one)
    with db._connect() as conn:
        topic = _loop_topic(conn, pid, stable)
    assert topic.tasks_total == 3, "rollup total must be the live generation (3), not 6"
    assert topic.tasks_done == 2, "rollup done must be the live generation (2), not 5"

    # graph_counts / counts_from_states (juggle_graph_status)
    counts = gs.graph_counts(db, pid)
    assert counts is not None
    assert counts["total"] == 3, "graph_counts total must be the live generation (3)"
    assert counts["verified"] == 2, "graph_counts done must be the live generation (2)"
    assert gs.format_progress(counts).startswith("2/3 done")


# ── the learnings-rollup COUNT scopes to the live generation too ────────────────
def test_learnings_count_scopes_to_live_generation(db):
    """§7.2 (RED today): the learnings-rollup COUNT (_task_state_counts) is a
    per-read-site scope target — it must count only the live generation's tasks,
    else a loop's learnings watermark drifts by every prior generation."""
    loop_id, pid, stable = _fire_to_r1(db)
    total, verified, in_progress = _task_state_counts(db, pid)
    assert total == 3, "learnings COUNT total must be the live generation (3), not 6"
    assert verified == 2, "learnings COUNT verified must be the live generation (2), not 5"
    assert in_progress == 1, "the one still-open r1 task"


# ── a pure-deliver single-generation loop reads N/N (guard; already holds) ───────
def test_pure_deliver_loop_reads_n_of_n(db):
    """§7.2: a delivery='deliver' loop's tasks terminate at 'delivered' (a success
    terminal), NOT 'verified'. A single-generation deliver loop with N delivered
    tasks must read N/N through BOTH rollups — scoping must never under-count a
    delivered generation. Already holds; pinned as a regression guard."""
    loop_id, res = _make_loop(db, delivery="deliver", n_tasks=3)
    pid, stable = res["project_id"], res["topic_id"]
    _set_gen_states(db, pid, loop_id, 0, "delivered")

    with db._connect() as conn:
        topic = _loop_topic(conn, pid, stable)
    assert (topic.tasks_done, topic.tasks_total) == (3, 3)

    counts = gs.graph_counts(db, pid)
    assert counts["total"] == 3 and counts["verified"] == 3
    assert gs.format_progress(counts).startswith("3/3 done")


# ── both rollups agree that cancelled counts as done ────────────────────────────
def test_both_rollups_agree_on_cancelled(db):
    """§7.2 reconcile (RED today): the executed graph_dag rollup counts 'cancelled'
    as done (state IN ('verified','delivered','cancelled')), but counts_from_states
    counted only TERMINAL_SUCCESS_STATES — so the two disagreed on a cancelled task.
    Align both on COMPLETION_TERMINAL_STATES. Incident 2026-07-05: a cancelled task
    read as done by the DAG panel but NOT by the progress string."""
    from dbops import db_graph, db_topics

    db_topics.create_topic(db, topic_id="T1", project_id="INBOX", title="T1")
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET state='integrating' WHERE id='T1' AND kind='topic'")
        conn.commit()
    states = ["verified", "cancelled", "open"]
    for i, st in enumerate(states):
        tid = f"T1-t{i}"
        db_graph.create_task(db, task_id=tid, project_id="INBOX", title=tid, prompt="x")
        db_graph.set_task_topic(db, tid, "T1")
        with db._connect() as conn:
            conn.execute("UPDATE nodes SET state=? WHERE id=? AND kind='task'", (st, tid))
            conn.commit()

    with db._connect() as conn:
        dag_done = next(t for t in _load_one(conn, "INBOX").tasks if t.id == "T1").tasks_done
    status_done = gs.counts_from_states(states)["verified"]

    assert dag_done == 2, "executed rollup counts verified+cancelled as done"
    assert status_done == 2, "counts_from_states must ALSO count cancelled as done"
    assert dag_done == status_done, "both rollups must agree on cancelled"
