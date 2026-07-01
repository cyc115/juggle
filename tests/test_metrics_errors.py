"""Orchestrator error-rate derivation (2026-06-30 orchestration-metrics Task 8)."""
import juggle_metrics_errors as me


def _run(task_id, status="completed"):
    return {"task_id": task_id, "status": status,
            "dispatched_at": "2026-06-30T12:00:00", "completed_at": "2026-06-30T12:01:00"}


def test_failed_and_rework(juggle_db):
    """2026-06-30 orchestration-metrics: error breakdown counts failed + re-dispatch rework."""
    runs = [_run("t1", "failed"), _run("t1"), _run("t2")]  # t1 dispatched 2× → 1 rework
    b = me.error_breakdown(juggle_db, runs)
    assert b["failed"] == 1 and b["rework"] == 1
    assert b["dispatches"] == 3
    assert b["rate"] == round(b["total"] / 3, 6)


def test_distinct_task_ids_no_rework(juggle_db):
    """2026-06-30 orchestration-metrics: a planned 2-node graph (distinct task_ids,
    each dispatched once) yields rework==0 — the key false-positive guard."""
    runs = [_run("nodeA"), _run("nodeB")]
    b = me.error_breakdown(juggle_db, runs)
    assert b["rework"] == 0


def test_blocked_counts_blocked_failed_nodes(juggle_db):
    """2026-06-30 orchestration-metrics: blocked = distinct task_id in blocked-failed."""
    from dbops import db_graph
    db_graph.create_task(juggle_db, task_id="t1", project_id="INBOX", title="t", prompt="p")
    db_graph.task_transition(juggle_db, "t1", "dep_fail")  # open -> blocked-failed
    b = me.error_breakdown(juggle_db, [_run("t1"), _run("t2")])
    assert b["blocked"] == 1
