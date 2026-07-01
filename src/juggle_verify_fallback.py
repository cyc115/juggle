"""juggle_verify_fallback — bounded verify-fallback for the self-heal loop.

Owns: the seam a task hits when it enters 'failed-verify' after complete-agent's
integrate ran the REAL ``verify_cmd`` and it was red. (R0) The seam currently
only ESCALATES — behaviour-identical to the pre-seam path. The bounded-retry
logic lands in a following step.

Must not own: task state semantics (dbops.db_graph — every write goes through
its writers), completion marking (juggle_cmd_agents_graph), or dispatch
(juggle_graph_dispatch — the tick owns re-dispatch).
"""

from __future__ import annotations

import logging

_log = logging.getLogger("juggle-verify-fallback")


def on_failed_verify(
    db, task, thread_uuid, session_id, *, verify_detail=None, escalate=None
) -> bool:
    """Handle a task that just entered 'failed-verify'.

    (R0) Behaviour-preserving seam: runs the caller's ``escalate`` callback (the
    HIGH action item + dependent propagation the non-fallback path always ran)
    and returns False (never retried). Bounded retry lands in the next step.
    """
    if escalate is not None:
        escalate()
    return False
