"""juggle_dispatch_stamp — the single seam that stamps "a task was just sent"
onto an agent row: last_task, last_send_task_pane_hash, last_send_task_at.

2026-07-13 completed-agents-never-decommission regression: the stalled/crashed
recovery re-dispatch (juggle_watchdog.execute_recovery) called mgr.send_task
directly and stamped only last_task on the DB row, never
last_send_task_pane_hash/last_send_task_at. The watchdog classifier
(juggle_watchdog.classify_pane_state) treats last_send_task_at IS NULL as
"never dispatched" -> state 'awaiting_dispatch', which the daemon poll loop
PERMANENTLY skips (juggle_watchdog_daemon._poll_once) — so a re-dispatched
agent that later stalled/crashed again was silently never recovered again.
Every send-task call site must stamp through this helper so the classifier
always sees a re-dispatch as dispatched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def record_dispatch(
    db: Any,
    agent_id: str,
    *,
    last_task: str,
    pane_hash: str | None,
    **extra: Any,
) -> None:
    """Stamp last_task/last_send_task_pane_hash/last_send_task_at (plus any
    caller-supplied extra agent fields) in one update_agent call."""
    db.update_agent(
        agent_id,
        last_task=last_task,
        last_send_task_pane_hash=pane_hash,
        last_send_task_at=datetime.now(timezone.utc).isoformat(),
        **extra,
    )
