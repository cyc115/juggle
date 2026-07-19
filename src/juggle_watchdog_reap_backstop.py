"""juggle_watchdog_reap_backstop — Fault-1 backstop (2026-07-13
completed-agents-never-decommission): reconcile a busy agent idle at its
prompt on a topic that never landed, but ONLY when a completion was actually
ATTEMPTED for its thread. Split out of juggle_watchdog_reap_done to keep that
module under the repo's LOC budget.

The sole discriminator is a DEAD-LETTERED agent_complete/agent_fail spool
event for the thread — NOT pane text. Pane text can't distinguish "attempted"
from "not yet attempted": the LIFECYCLE section of every coder's dispatched
prompt always shows the literal `agent complete <id> ...` command in
scrollback, whether or not the agent ever ran it. A dead-letter file, by
contrast, only exists when apply_event durably failed on a REAL completion or
failure call.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from dbops import event_kinds as _ek

_log = logging.getLogger("juggle-watchdog")

_MIN_AGE_ENV = "JUGGLE_REAP_BACKSTOP_MIN_AGE_SECS"
_MIN_AGE_DEFAULT_SECS = 300.0


def resolve_min_age_secs(override: float | None) -> float:
    """``override`` wins when given (tests); else env
    ``JUGGLE_REAP_BACKSTOP_MIN_AGE_SECS`` (default 300s)."""
    if override is not None:
        return override
    raw = os.environ.get(_MIN_AGE_ENV)
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return _MIN_AGE_DEFAULT_SECS


_ATTEMPTED_COMPLETION_TYPES = ("agent_complete", "agent_fail")


def find_attempted_completion(thread_id: str) -> dict | None:
    """Newest dead-lettered agent_complete/agent_fail spool event whose
    args.thread_id matches ``thread_id``, or None. (spool_event_if_agent
    always writes the top-level agent_id/thread_id as "" — the real thread id
    lives in args.)

    agent_fail is included alongside agent_complete (2026-07-18 dead-letter
    672c4a14): a dead-lettered agent_fail is just as much an ATTEMPTED
    finalization as a dead-lettered agent_complete — omitting it left an
    agent whose failure call dead-lettered stuck at status='busy' forever,
    since this is the sole signal the Fault-1 backstop uses to reconcile it."""
    from juggle_spool_paths import spool_dead_dir

    dead_dir = spool_dead_dir()
    if not dead_dir.exists():
        return None
    found: dict | None = None
    for path in sorted(dead_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if payload.get("type") not in _ATTEMPTED_COMPLETION_TYPES:
            continue
        if (payload.get("args") or {}).get("thread_id") != thread_id:
            continue
        found = payload  # filenames sort oldest-first; keep the newest match
    return found


def _parse_iso(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def event_age_secs(payload: dict) -> float | None:
    dt = _parse_iso(payload.get("created_at"))
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def dead_letter_matches_agent(dead_letter: dict, agent: dict) -> bool:
    """True iff ``agent`` was already busy (this assignment) by the time
    ``dead_letter`` was created — i.e. this is the SAME agent whose completion
    attempt dead-lettered, not a freshly (re-)dispatched agent that merely
    inherited a stale dead-letter left over from a PRIOR attempt on this
    thread (``cas_assign_agent``/spawn always re-stamp ``busy_since`` on every
    new assignment, so a later assignment's busy_since postdates an earlier
    attempt's dead-letter)."""
    busy_since = _parse_iso(agent.get("busy_since"))
    created_at = _parse_iso(dead_letter.get("created_at"))
    if busy_since is None or created_at is None:
        return False
    return busy_since <= created_at


def reconcile_failed_landing(
    db: Any, agent: dict, thread_id: str, label: str, session_id: str, dead_letter: dict,
) -> None:
    """Reconcile the AGENT only — return it to the pool and close its ledger
    run — and push a needs-judgment action item per the triage ladder
    (CLAUDE.md: unclassifiable machinery failure -> orchestrator). Deliberately
    does NOT touch the topic's state: work may not actually be landed, so
    silently marking it verified/done would hide a real failure. Best-effort;
    never raises."""
    agent_id = agent["id"]

    db.update_agent(
        agent_id,
        status="idle",
        assigned_thread=None,
        last_task=None,
        last_send_task_pane_hash=None,
        last_send_task_at=None,
        watchdog_retried=0,
        model=None,
    )

    try:
        from juggle_agent_reap import close_thread_run_backstop
        close_thread_run_backstop(
            db, thread_id, output="completed-agent reaped (completion failed to land)"
        )
    except Exception:
        pass

    try:
        db.add_action_item(
            thread_id=thread_id,
            message=(
                f"🛑 [{label}] agent completion FAILED TO LAND (dead-lettered "
                f"{dead_letter.get('type', 'agent_complete')} {dead_letter.get('uuid', '?')}) "
                f"— agent released to "
                f"pool, topic left as-is (not marked done). Needs judgment: inspect "
                f"`juggle spool list-dead` and replay or re-dispatch."
            ),
            type_="failure",
            priority="high",
        )
    except Exception:
        pass

    try:
        db.emit_event(
            thread_id=thread_id,
            session_id=session_id,
            kind=_ek.WATCHDOG_RECOVERY,
            message=(f"[Watchdog] [{label}] completed agent idle on a non-landed "
                     f"topic with an attempted-completion dead-letter — released to "
                     f"pool, pushed to orchestrator (machinery error)"),
        )
    except Exception:
        pass

    _log.warning(
        "Watchdog: reaped completed agent %s [%s] — completion failed to land, "
        "pushed to orchestrator",
        agent_id[:8], label,
    )
