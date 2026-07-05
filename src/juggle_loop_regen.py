"""juggle_loop_regen — reconstruct a loop's template + atomically regenerate its
next run-seq iteration (loop-entity, extracted from juggle_loop_fire for the LOC
gate, 2026-07-04).

Cohesive seam: given a loop, rebuild the normalized template topic from its
canonical r0 nodes, then bump run_seq AND materialize the next iteration's nodes
in ONE transaction (rollback-on-error). Kept separate from the firing/scheduling
policy (juggle_loop_fire) and the failure-surfacing choke point.
"""
from __future__ import annotations

from dbops.schema import _now as _now_ts
from juggle_loop_instantiate import instantiate_topic


def _reconstruct_topic(db, loop: dict) -> dict:
    """Rebuild the normalized template topic from the loop's canonical r0 nodes.

    The loops row does not store the template JSON (no column in V1); the original
    iteration-0 nodes ARE the template. Strip the ``<L#>-r0-`` prefix off each node
    id to recover the base template ids, then hand a normalized topic dict to the
    shared ``instantiate_topic`` writer under the new run-seq prefix."""
    loop_id, project_id = loop["id"], loop["project_id"]
    src_prefix = f"{loop_id}-r0-"
    base = len(src_prefix)
    with db._connect() as conn:
        topic_row = conn.execute(
            "SELECT id, title, objective, role, delivery FROM nodes "
            "WHERE kind='topic' AND project_id=? AND id LIKE ? LIMIT 1",
            (project_id, src_prefix + "%"),
        ).fetchone()
        if topic_row is None:
            raise ValueError(f"loop {loop_id!r} has no r0 template topic to re-fire")
        task_rows = conn.execute(
            "SELECT id, title, objective, verify_cmd, role, delivery FROM nodes "
            "WHERE kind='task' AND project_id=? AND id LIKE ? ORDER BY created_at, id",
            (project_id, src_prefix + "%"),
        ).fetchall()
        deps = {}
        for tr in task_rows:
            drows = conn.execute(
                "SELECT depends_on_id FROM node_edges WHERE node_id=? AND kind='dep'",
                (tr["id"],),
            ).fetchall()
            # Only strip the r0 prefix off deps that actually carry it (code-review
            # #5): a dep pointing outside the loop's r0 namespace would otherwise be
            # mis-stripped into a bogus re-prefixed edge. V1 single-topic templates
            # are self-contained so this can't happen today — defensive hardening.
            deps[tr["id"]] = [d[0][base:] for d in drows if d[0].startswith(src_prefix)]
    tasks = [{
        "id": tr["id"][base:], "title": tr["title"], "prompt": tr["objective"],
        "role": tr["role"], "delivery": tr["delivery"], "verify_cmd": tr["verify_cmd"],
        "deps": deps[tr["id"]],
    } for tr in task_rows]
    return {
        "id": topic_row["id"][base:], "title": topic_row["title"],
        "objective": topic_row["objective"], "role": topic_row["role"],
        "delivery": topic_row["delivery"], "tasks": tasks,
    }


def _fire_next_iteration_atomic(db, loop: dict) -> int:
    """Bump run_seq AND instantiate the iteration in ONE transaction; return the new
    run_seq.

    The seq bump and the node instantiation commit together (code-review #2, RED-pin
    test_failed_instantiate_rolls_back_seq_bump): if instantiate raises, the seq bump
    rolls back too, so there is NEVER a bumped run_seq with zero task nodes — an empty
    iteration would otherwise read as a phantom 'success' next window, silently
    resetting the circuit-breaker and masking the failure."""
    topic = _reconstruct_topic(db, loop)  # reads committed r0 nodes (own conn)
    conn = db._connect()
    try:
        row = conn.execute(
            "UPDATE loops SET run_seq = run_seq + 1, updated_at = ? WHERE id = ? "
            "RETURNING run_seq",
            (_now_ts(), loop["id"]),
        ).fetchone()
        new_seq = row[0]
        prefix = f"{loop['id']}-r{new_seq}-"
        instantiate_topic(db, conn, project_id=loop["project_id"], prefix=prefix, topic=topic)
        conn.commit()
        return new_seq
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
