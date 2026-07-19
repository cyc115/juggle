"""juggle_graph_reintegrate_liveness — bound-agent liveness guard for the
reintegrate sweep.

Extracted from juggle_graph_reintegrate (LOC gate, 2026-07-19: the Bug#1
still-broken fix's wrapper-outcome propagate call pushed the driver past its
300-line budget) — owns ONLY the "is a topic's bound agent still legitimately
mid-finalize" check (_bound_agent_blocks + its two probes). Re-exported from
juggle_graph_reintegrate (same names/signatures) so existing callers/tests
keep importing from there unchanged. Must not own the sweep's own re-drive/
backoff decisions (juggle_graph_reintegrate).
"""

from __future__ import annotations

from datetime import datetime

REINTEGRATE_STALE_BOUND_SECS = 1800  # 30 min escape-hatch backstop


def _has_live_bound_agent(db, thread_id: str | None) -> bool:
    if not thread_id:
        return False
    try:
        return db.get_agent_by_thread(thread_id) is not None
    except Exception:
        return False


def _completion_recorded(db, thread_id: str | None) -> bool:
    """True iff the agent_runs ledger records a completion for this thread's
    NEWEST run — not "any" (RC3 parity), so a prior dispatch can't mask an open re-dispatch."""
    if not thread_id:
        return False
    try:
        runs = db.get_runs(thread_id=thread_id, limit=1)
    except Exception:
        return False
    return bool(runs) and runs[0].get("status") == "completed"


def _bound_agent_blocks(db, topic: dict, thread_id: str | None, now: datetime) -> bool:
    """A live bound agent normally blocks re-drive (may be mid-finalize). Escape
    hatches (integrate-wedge #2, RC2), for when the inline gate died so the agent
    never releases: RECORDED completion → NON-blocking; else stale → re-drive."""
    from juggle_graph_reintegrate import _secs_since  # deferred: avoids the cycle

    if not _has_live_bound_agent(db, thread_id):
        return False
    if _completion_recorded(db, thread_id):
        return False
    if _secs_since(topic.get("updated_at"), now) >= REINTEGRATE_STALE_BOUND_SECS:
        import logging

        logging.getLogger("juggle-graph-reintegrate").warning(
            "reintegrate: topic %s bound + no completion, stale > %ds — "
            "presuming dead finalize, re-driving", topic.get("id"),
            REINTEGRATE_STALE_BOUND_SECS)
        return False
    return True
