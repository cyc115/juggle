"""juggle_loop_regen — reconstruct a loop's template + ATOMICALLY reopen-regenerate
its next run-seq TASK generation under the loop's STABLE topic (loop-entity V2
stable-topic model, §0b; extracted from juggle_loop_fire for the LOC gate,
2026-07-04).

Cohesive seam: given a loop, rebuild the normalized template from its canonical r0
nodes, then — under the loop's ONE long-lived topic — reopen the topic if terminal,
CLEAR its durable integrate state, and materialize the next iteration's task
generation, ALL in one transaction. Kept separate from the firing/scheduling policy
(juggle_loop_fire) and the failure-surfacing choke point.
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
        topic_rows = conn.execute(
            "SELECT id, title, objective, role, delivery FROM nodes "
            "WHERE kind='topic' AND project_id=? AND id LIKE ? "
            "ORDER BY created_at, id",
            (project_id, f"{loop_id}-%"),
        ).fetchall()
        if not topic_rows:
            raise ValueError(f"loop {loop_id!r} has no stable topic to re-fire")
        if len(topic_rows) > 1:
            # P4b lands create-time MULTI-topic + the cross-topic vault handoff;
            # multi-topic re-fire REGENERATION (reopen every stable topic + re-wire the
            # crossing edges each fire) is a deferred follow-up. Fail LOUD rather than
            # silently regenerate only the first topic — that would drop topics and
            # leave the crossing edges dangling (a task wedged at never-deps-ready).
            # Raising here routes through fire_due_loops' never-swallow choke point.
            raise ValueError(
                f"loop {loop_id!r} has {len(topic_rows)} stable topics — multi-topic "
                f"re-fire regeneration is not yet implemented (deferred P4b follow-up); "
                f"refusing to regenerate a partial generation"
            )
        topic_row = topic_rows[0]
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


def _reset_topic_integrate_state(db, conn, topic_id: str) -> None:
    """Clear ALL of the stable topic's durable integrate state so generation N+1
    starts clean, ON the caller's txn. ``reopen`` PRESERVES integrate state
    (db_topics_marking.py:84 — "reopen resurrects"), so with a stable topic gen N's
    state poisons N+1 unless every seam is cleared here (§0b consequence 2):

      * ``merged_sha`` — else the verified gate passes gen N+1 against gen N's stale
        (still-ancestor) sha;
      * ``fail_envelope`` — the repair gate reads ``attempts_total`` off it and
        ``BACKSTOP_TOTAL_PER_TOPIC=3`` overrides everything, so a topic that ever
        needed a repair would refuse / silently halve N+1's repair budget;
      * ``pending_merged_sha``/``pending_merged_repo`` — an unproven sha the
        reconcile sweep's ``_heal_merged_sha`` could later promote onto N+1;
      * the reintegrate backoff counters (``db_reintegrate.forget``);
      * the ``submitted_rev``/``verified_at`` audit fields.
    """
    conn.execute(
        "UPDATE nodes SET merged_sha=NULL, pending_merged_sha=NULL, "
        "pending_merged_repo=NULL, fail_envelope=NULL, submitted_rev=NULL, "
        "verified_at=NULL, updated_at=? WHERE id=? AND kind='topic'",
        (_now_ts(), topic_id),
    )
    from dbops import db_reintegrate
    db_reintegrate.forget(db, topic_id, conn=conn)


def _reopen_and_reset_if_terminal(db, conn, topic_id: str) -> None:
    """Make the stable topic fire-ready for the next generation, ON the caller's txn:
    ``open`` already is; a reopenable terminal is reopened + its durable integrate
    state cleared (``verified→(reopen)→open→integrating`` is legal,
    db_topics_marking.py:78-89).

    FAIL LOUD on any other state (code review 2026-07-04). ``iteration_outcome``
    classifies fire-eligibility on TASK states, but reopen/reset gates on the TOPIC
    state — and the two can disagree (gen-N's task rows terminal while the topic-
    level integrate is still 'integrating', or a non-reopenable terminal like
    'done'/'cancelled'/'archived'). Instantiating gen N+1 under such a NON-'open'
    topic would wedge it (topic_ready_eligible requires state='open') AND skip the
    integrate-state reset — leaking gen N into N+1. Raising rolls the whole atomic
    fire back and routes it through fire_due_loops' never-swallow choke point instead
    of silently wedging."""
    topic = db_topics.get_topic(db, topic_id, conn=conn)
    if topic is None:
        raise ValueError(f"loop stable topic not found: {topic_id!r}")
    state = topic["state"]
    if state == "open":
        return  # first fire (r0 create) / already reopened — instantiate directly
    if state not in _REOPENABLE_TERMINALS:
        raise ValueError(
            f"loop topic {topic_id!r} not fire-ready for regeneration "
            f"(state={state!r}); expected 'open' or a reopenable terminal — the "
            f"prior generation's topic is still active or non-reopenable"
        )
    db_topics.topic_transition(db, topic_id, "reopen", conn=conn)
    _reset_topic_integrate_state(db, conn, topic_id)


def _fire_next_iteration_atomic(db, loop: dict) -> int:
    """ATOMICALLY reopen-regenerate the loop's next iteration under its STABLE topic;
    return the new run_seq.

    Bump run_seq, reopen+reset the stable topic if terminal, and instantiate the new
    task generation — ALL in ONE transaction (rollback-on-error). Two invariants ride
    on the atomicity:
      * a fire that reopens the topic then FAILS instantiation rolls the reopen back
        to the prior terminal (§0b consequence 1) — otherwise the topic wedges
        permanently in 'open' (no reopen edge; kind='topic' never auto-terminalizes);
      * the seq bump rolls back with the instantiate (code-review #2, RED-pin
        test_failed_instantiate_rolls_back_seq_bump) — never a bumped run_seq with
        zero task nodes."""
    topic = _reconstruct_topic(db, loop)  # reads committed r0 nodes (own conn)
    stable_topic_id = f"{loop['id']}-{topic['id']}"
    conn = db._connect()
    try:
        row = conn.execute(
            "UPDATE loops SET run_seq = run_seq + 1, updated_at = ? WHERE id = ? "
            "RETURNING run_seq",
            (_now_ts(), loop["id"]),
        ).fetchone()
        new_seq = row[0]
        _reopen_and_reset_if_terminal(db, conn, stable_topic_id)
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
