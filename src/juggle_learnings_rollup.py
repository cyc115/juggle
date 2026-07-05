"""juggle_learnings_rollup — graph-node learnings rollup + adoption nudge.

Owns the watchdog-side push of the learnings primitive (spec 2026-07-03 §5 +
Final revisions 3/4/7). Both entry points run ONCE PER PROJECT inside the
serialized ``poll_unlanded_topics`` sweep (the graph_tick path, per Final
revision 4) — NEVER in the concurrent ``cmd_complete_agent`` process, so the
count is race-free and restart-safe by construction:

  * ``maybe_rollup_learnings`` — derive COUNT(verified tasks) each tick, compare
    to the per-project ``learnings_rollup_mark:<p>`` watermark. Emit a
    ``learnings_rollup`` event (routed to the orchestrator) when ≥10 new
    completions accrued OR the project graph just fully completed (the latter
    fires at most once via the ``learnings_rollup_final:<p>`` sentinel). Payload
    = learnings WRITTEN since the last rollup; none → advance the mark silently.
  * ``nudge_missing_learnings`` — a NON-BLOCKING adoption nudge: a verified topic
    that has dependents but recorded ZERO learnings AND has an empty/stub handoff
    gets one low-priority action item (Final revision 7). Never a hard gate.

Neither function ever raises — a failure here must not abort the tick.
"""
from __future__ import annotations

import logging

from dbops import db_topics
from dbops import event_kinds as _ek
from dbops.schema import _now
from dbops.terminal_states import IN_FLIGHT_STATES as _IN_PROGRESS

_log = logging.getLogger("juggle-learnings-rollup")

# ≥10 new completions since the watermark → rollup (spec §5).
ROLLUP_THRESHOLD = 10
# A verified topic with a handoff shorter than this (post-strip) counts as a
# "stub" for the adoption nudge (Final revision 7). The handoff gate already
# forces SOME handoff on a node with dependents; a near-empty one is a stub.
_STUB_HANDOFF_LEN = 10

# Task states that are still in-flight (not a completion) are the canonical
# IN_FLIGHT_STATES (imported above as _IN_PROGRESS, Phase 0). Everything else
# (verified / failed-* / blocked-failed) is terminal → the project is "done"
# once none of these remain. INVERSE set: a later terminal like 'delivered' must
# NOT be in here, so it counts as done rather than in-progress-forever.


def _mark_key(project_id: str) -> str:
    return f"learnings_rollup_mark:{project_id}"


def _ts_key(project_id: str) -> str:
    return f"learnings_rollup_ts:{project_id}"


def _final_key(project_id: str) -> str:
    return f"learnings_rollup_final:{project_id}"


def _nudge_key(topic_id: str) -> str:
    return f"learnings_nudge_done:{topic_id}"


def _session_id(db) -> str:
    try:
        with db._connect() as conn:
            return db._get_session_key(conn, "session_id") or ""
    except Exception:
        return ""


def _task_state_counts(db, project_id: str) -> tuple[int, int, int]:
    """(total, verified, in_progress) task-node counts for the project.

    For a LOOP project the counts scope to the live run_seq generation
    (``<loop_id>-r<seq>-%``, §7.2) so the learnings watermark tracks the current
    generation rather than drifting by every accumulated prior generation; non-loop
    projects are unscoped (``loop_like`` is None)."""
    from dbops.loops import live_generation_like

    with db._connect() as conn:
        loop_like = live_generation_like(conn, project_id)
        gen_and = " AND id LIKE ?" if loop_like else ""
        gen_p = (loop_like,) if loop_like else ()
        total = conn.execute(
            f"SELECT COUNT(*) FROM nodes WHERE kind='task' AND project_id=?{gen_and}",
            (project_id, *gen_p),
        ).fetchone()[0]
        verified = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind='task' AND project_id=?"
            f"{gen_and} "
            "AND state='verified'",
            (project_id, *gen_p),
        ).fetchone()[0]
        placeholders = ",".join("?" * len(_IN_PROGRESS))
        in_progress = conn.execute(
            f"SELECT COUNT(*) FROM nodes WHERE kind='task' AND project_id=? "
            f"AND state IN ({placeholders}){gen_and}",
            (project_id, *sorted(_IN_PROGRESS), *gen_p),
        ).fetchone()[0]
    return total, verified, in_progress


def maybe_rollup_learnings(db, project_id: str) -> None:
    """Per-project, per-tick learnings rollup (serialized — graph_tick only).

    Race-free: the completion count is DERIVED via COUNT(verified) each tick;
    the settings watermark stores only the last-rollup count + timestamp
    (Final revision 4). Never raises.
    """
    try:
        from dbops import db_graph

        total, verified, in_progress = _task_state_counts(db, project_id)
        if total == 0:
            return
        mark = int(db.get_setting(_mark_key(project_id)) or "0")
        fully_complete = in_progress == 0
        final_fired = bool(db.get_setting(_final_key(project_id)))

        threshold_hit = (verified - mark) >= ROLLUP_THRESHOLD
        complete_edge = fully_complete and not final_fired
        if not (threshold_hit or complete_edge):
            return

        # Payload = learnings written since the last rollup: non-empty learnings
        # on nodes verified after the stored timestamp watermark.
        last_ts = db.get_setting(_ts_key(project_id)) or ""
        result = db_graph.read_learnings(db, project_id=project_id)
        new_items = [
            it
            for it in result["items"]
            if it.get("completed_at") and it["completed_at"] > last_ts
        ]

        if new_items:
            lines = "\n".join(f"{it['node_id']}: {it['text']}" for it in new_items)
            db.emit_event(
                thread_id=None,
                session_id=_session_id(db),
                kind=_ek.LEARNINGS_ROLLUP,
                message=(
                    f"⬢ learnings rollup ({project_id}): "
                    f"{len(new_items)} new learning(s) to triage\n{lines}"
                ),
            )
        # Advance the watermark whether or not anything was emitted — a quiet
        # stretch just moves the mark forward (spec §5: "none → advance silently").
        db.set_setting(_mark_key(project_id), str(verified))
        db.set_setting(_ts_key(project_id), _now())
        if fully_complete:
            db.set_setting(_final_key(project_id), _now())
    except Exception:
        _log.exception("learnings rollup: failed for project %s — skipping", project_id)


def _topics_with_dependents(db, project_id: str) -> set[str]:
    """Topic ids in the project that at least one OTHER topic depends on."""
    deps: set[str] = set()
    for topic in db_topics.list_topics(db, project_id):
        for dep in db_topics.derived_topic_deps(db, topic["id"]):
            deps.add(dep)
    return deps


def _is_stub_handoff(handoff) -> bool:
    return not handoff or len(handoff.strip()) < _STUB_HANDOFF_LEN


def nudge_missing_learnings(db, project_id: str) -> None:
    """Non-blocking adoption nudge (Final revision 7): a verified topic with
    dependents but zero learnings AND a stub handoff earns ONE low-priority
    action item. Idempotent via the ``learnings_nudge_done:<t>`` sentinel — the
    successor still gets nothing to read, but we never re-file. Never raises.
    """
    try:
        from dbops import db_graph

        with_dependents = _topics_with_dependents(db, project_id)
        if not with_dependents:
            return
        for topic in db_topics.list_topics(db, project_id):
            tid = topic["id"]
            if topic["state"] != "verified" or tid not in with_dependents:
                continue
            if db.get_setting(_nudge_key(tid)):
                continue
            if not _is_stub_handoff(topic.get("handoff")):
                continue
            if db_graph.read_learnings(db, topic_id=tid)["with_learnings"] > 0:
                continue
            db.add_action_item(
                thread_id=None,
                message=(
                    f"💡 Topic {tid} ({topic.get('title', tid)}) completed with no "
                    f"learnings and a thin handoff — successors inherit no "
                    f"gotchas/constraints. Encourage `juggle graph learn <node-id>` "
                    f"on future tasks."
                ),
                type_="manual_step",
                priority="low",
            )
            db.set_setting(_nudge_key(tid), _now())
    except Exception:
        _log.exception("learnings nudge: failed for project %s — skipping", project_id)
