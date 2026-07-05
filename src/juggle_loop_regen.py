"""juggle_loop_regen — reconstruct a loop's template + regenerate its next run-seq
TASK generation under the loop's STABLE topic (loop-entity V2 stable-topic model,
§0b; extracted from juggle_loop_fire for the LOC gate, 2026-07-04).

Cohesive seam: given a loop, rebuild the normalized template from its canonical r0
nodes, then — under the loop's ONE long-lived topic — reopen the topic if it is
terminal and materialize the next iteration's task generation. Kept separate from
the firing/scheduling policy (juggle_loop_fire) and the failure-surfacing choke
point.
"""
from __future__ import annotations

from dbops import db_topics
from dbops.schema import _now as _now_ts
from juggle_loop_instantiate import instantiate_generation

# The terminal states with a legal ``reopen`` edge (db_node_machine): a loop topic
# parks at verified/delivered between fires (or a failure terminal after a bad
# generation) and must reopen to host the next generation.
_REOPENABLE_TERMINALS = frozenset({
    "verified", "delivered", "integrated-unlanded",
    "failed-exec", "failed-integration", "failed-verify", "blocked-failed",
})


def _reconstruct_topic(db, loop: dict) -> dict:
    """Rebuild the normalized template from the loop's canonical r0 nodes.

    The loops row does not store the template JSON; the original iteration-0 nodes
    ARE the template. The topic is the loop's ONE stable ``kind='topic'`` node
    (``<L#>-<topic>``, no run prefix); the r0 TASK nodes (``<L#>-r0-<task>``) carry
    the task template. Strip the run prefix off task ids / the ``<L#>-`` prefix off
    the topic id to recover the base template ids."""
    loop_id, project_id = loop["id"], loop["project_id"]
    src_prefix = f"{loop_id}-r0-"
    base = len(src_prefix)
    with db._connect() as conn:
        topic_row = conn.execute(
            "SELECT id, title, objective, role, delivery FROM nodes "
            "WHERE kind='topic' AND project_id=? ORDER BY created_at, id LIMIT 1",
            (project_id,),
        ).fetchone()
        if topic_row is None:
            raise ValueError(f"loop {loop_id!r} has no stable topic to re-fire")
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
            # mis-stripped into a bogus re-prefixed edge.
            deps[tr["id"]] = [d[0][base:] for d in drows if d[0].startswith(src_prefix)]
    tasks = [{
        "id": tr["id"][base:], "title": tr["title"], "prompt": tr["objective"],
        "role": tr["role"], "delivery": tr["delivery"], "verify_cmd": tr["verify_cmd"],
        "deps": deps[tr["id"]],
    } for tr in task_rows]
    return {
        "id": topic_row["id"][len(loop_id) + 1:], "title": topic_row["title"],
        "objective": topic_row["objective"], "role": topic_row["role"],
        "delivery": topic_row["delivery"], "tasks": tasks,
    }


def _reopen_if_terminal(db, topic_id: str) -> None:
    """Reopen the stable topic when it is parked in a terminal state so the next
    generation can run. SCAFFOLD: reopens on its own connection (commits), no
    integrate-state reset yet — the P3a pins catch both gaps."""
    topic = db_topics.get_topic(db, topic_id)
    if topic and topic["state"] in _REOPENABLE_TERMINALS:
        db_topics.topic_transition(db, topic_id, "reopen")


def _fire_next_iteration_atomic(db, loop: dict) -> int:
    """Regenerate the loop's next iteration under its STABLE topic; return the new
    run_seq.

    Reopen the stable topic if terminal, then bump run_seq AND instantiate the new
    task generation. The seq bump and node instantiation commit together
    (code-review #2, RED-pin test_failed_instantiate_rolls_back_seq_bump): if
    instantiate raises, the seq bump rolls back too, so there is NEVER a bumped
    run_seq with zero task nodes."""
    topic = _reconstruct_topic(db, loop)  # reads committed r0 nodes (own conn)
    stable_topic_id = f"{loop['id']}-{topic['id']}"
    _reopen_if_terminal(db, stable_topic_id)
    conn = db._connect()
    try:
        row = conn.execute(
            "UPDATE loops SET run_seq = run_seq + 1, updated_at = ? WHERE id = ? "
            "RETURNING run_seq",
            (_now_ts(), loop["id"]),
        ).fetchone()
        new_seq = row[0]
        gen_prefix = f"{loop['id']}-r{new_seq}-"
        instantiate_generation(
            db, conn, project_id=loop["project_id"], topic_id=stable_topic_id,
            gen_prefix=gen_prefix, tasks=topic["tasks"],
        )
        conn.commit()
        return new_seq
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
