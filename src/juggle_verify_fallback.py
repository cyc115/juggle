"""juggle_verify_fallback — bounded verify-fallback for the self-heal loop.

Owns: the seam a task hits when it enters 'failed-verify' after complete-agent's
integrate ran the REAL ``verify_cmd`` and it was red. The fallback is a bounded
retry with FRESH CONTEXT: while ``verify_retries < N`` (config
``JUGGLE_VERIFY_FALLBACK_RETRIES``, default 1) the task's retry counter is
incremented, the prior verify FAILURE OUTPUT is stored for prompt injection, and
the task is RESET to 'ready' so the watchdog tick re-dispatches a fresh agent.
The tick then re-runs the real ``verify_cmd`` exactly as it already does — this
is the SAFETY RAIL: the system ALWAYS re-runs the real predicate, so there is no
verdict CLI, no evidence artifact, no rubber-stamp surface. On retry exhaustion
the task stays 'failed-verify' and escalates via the SAME HIGH action item the
non-fallback path always raised (terminal).

Must not own: task state semantics (dbops.db_graph — every write goes through
its writers), completion marking (juggle_cmd_agents_graph), or dispatch
(juggle_graph_dispatch — the tick owns re-dispatch).
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger("juggle-verify-fallback")

# Default bounded-retry budget (one retry). Overridable per-run via the
# JUGGLE_VERIFY_FALLBACK_RETRIES env var or config.json's top-level
# ``verify_fallback_retries`` key.
DEFAULT_VERIFY_FALLBACK_RETRIES = 1


def get_verify_fallback_retries() -> int:
    """Resolve the bounded-retry budget N (env override > config > default).

    Fail-soft: a non-integer / negative value falls back to the default so a
    typo can never spin an unbounded loop.
    """
    raw = os.environ.get("JUGGLE_VERIFY_FALLBACK_RETRIES")
    if raw is None:
        try:
            from juggle_settings import get
            raw = get("verify_fallback_retries", DEFAULT_VERIFY_FALLBACK_RETRIES)
        except Exception:
            return DEFAULT_VERIFY_FALLBACK_RETRIES
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_VERIFY_FALLBACK_RETRIES
    return n if n >= 0 else DEFAULT_VERIFY_FALLBACK_RETRIES


def on_failed_verify(
    db, task, thread_uuid, session_id, *, verify_detail=None, escalate=None
) -> bool:
    """Handle a task that just entered 'failed-verify'.

    Returns True when a bounded RETRY was scheduled (counter bumped, failure
    output stored, task reset to 'ready' for a fresh re-dispatch); False when
    the retry budget is exhausted and the task was ESCALATED (terminal).

    ``escalate`` is the behaviour-preserving escalation callback (the HIGH
    action item + dependent propagation the non-fallback path always ran); it
    is invoked exactly once on exhaustion.
    """
    from dbops import db_graph

    max_retries = get_verify_fallback_retries()
    retries = int(task.get("verify_retries") or 0)

    if retries < max_retries:
        # (1) counter + (2) stored prior failure output, then RESET to ready so
        # the tick re-dispatches a FRESH agent. reload → open; recompute_ready
        # promotes it back to ready (all deps are verified — it already ran) and
        # pokes the watchdog.
        db_graph.bump_verify_retry(db, task["id"], verify_detail)
        db_graph.task_transition(db, task["id"], "reload")  # failed-verify → open
        db_graph.recompute_ready(db, task["project_id"])     # open → ready
        db.add_notification_v2(
            thread_id=thread_uuid,
            message=(
                f"⟳ graph task {task['id']} verify failed — retry "
                f"{retries + 1}/{max_retries} scheduled (fresh agent)"
            ),
            session_id=session_id,
        )
        _log.info(
            "verify fallback: task %s reset to ready (retry %d/%d)",
            task["id"], retries + 1, max_retries,
        )
        return True

    # Exhausted — terminal. Run the caller's behaviour-preserving escalation.
    if escalate is not None:
        escalate()
    _log.warning(
        "verify fallback: task %s exhausted %d retries — escalated",
        task["id"], max_retries,
    )
    return False
