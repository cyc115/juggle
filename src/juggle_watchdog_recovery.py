"""juggle_watchdog_recovery — stalled/crashed agent decommission + re-dispatch.

Owns: execute_recovery (batch recovery: decommission a dead/stalled/crashed
agent and, if eligible, re-dispatch its last task to a fresh agent).
Must not own: single-agent inspection (inspect_agent), orphan scanning
(check_orphaned_threads), classifier constants (see juggle_watchdog.py).

Re-exported by juggle_watchdog.py so existing imports
``from juggle_watchdog import execute_recovery`` continue to work.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from dbops import event_kinds as _ek

_log = logging.getLogger(__name__)


def execute_recovery(
    db: Any,
    mgr: Any,
    agent: dict,
    pane_content: str,
    *,
    recovery_dir: Path,
    session_id: str,
) -> None:
    """Decommission a stalled/crashed agent and (if eligible) re-dispatch it."""
    # Import classifier/recovery helpers + cascade-dedup state from juggle_watchdog
    # to avoid duplication. juggle_watchdog imports this module at top level, so
    # this must stay deferred to resolve the circular reference (mirrors
    # juggle_watchdog_inspect.py / juggle_watchdog_orphans.py).
    import juggle_watchdog as _wdog

    agent_id = agent["id"]

    # DA-6: Recheck agent status from DB to guard against TOCTOU race; use live
    # record for all subsequent reads so a concurrent release can't mislead us.
    live = db.get_agent(agent_id)
    if live is None or live.get("status") != "busy":
        _log.info(
            "Watchdog: recovery aborted for %s — agent no longer busy", agent_id[:8]
        )
        return

    # Policy: never kill a live agent. Only recover dead / never-fired panes.
    pane_exists = mgr.verify_pane(live["pane_id"])

    if _wdog._agent_is_non_interactive(live):
        # One-shot agent: use PID liveness, not pane markers.
        # Pane markers are meaningless for non-interactive harnesses (no Claude UI).
        from juggle_tmux import oneshot_agent_alive as _oneshot_alive
        if _oneshot_alive(live):
            # Still running — no recovery needed.
            _log.info(
                "Watchdog: non-interactive agent %s is alive (PID check) — skipping",
                agent_id[:8],
            )
            return
        # Dead one-shot + pane still exists but process died → treat as never_fired
        # Fall through to recovery below.
        if not pane_exists:
            agent_state = "dead"
        else:
            # Process died but pane may still show shell — proceed to recovery.
            agent_state = "never_fired"
    else:
        agent_state = _wdog._classify_agent_state(pane_content, pane_exists)
        if agent_state == "alive_slow":
            _t = live.get("assigned_thread")
            if _t:
                _thread = db.get_thread(_t)
                if _thread and _thread.get("state") == "done":
                    _log.info(
                        "Watchdog: agent %s alive_slow but thread %s is closed — idling agent",
                        agent_id[:8], _t[:8],
                    )
                    db.update_agent(agent_id, status="idle", assigned_thread=None)
                    return
            ctx_pct = _wdog._parse_context_pct(pane_content)
            active = _wdog._has_active_spinner(pane_content)
            action = _wdog.recovery_action(
                context_pct=ctx_pct,
                has_active_spinner=active,
                is_dead=False,
                never_fired=False,
            )
            if action == "recycle":
                _log.warning(
                    "Watchdog: alive_slow at high context (%.0f%%) — recycling to fresh agent",
                    ctx_pct * 100,  # type: ignore[operator]
                )
                # fall through to decommission + re-dispatch below
            elif action == "none":
                _log.info(
                    "Watchdog: alive_slow with active spinner — leaving agent %s alone",
                    agent_id[:8],
                )
                return
            else:
                _wdog.nudge_and_notify(db, mgr, live, pane_content)
                return

    thread_id = live.get("assigned_thread")
    role = live.get("role", "researcher")
    model = None  # Fix 4: always use current config model; never forward stale snapshot model
    last_task = live.get("last_task")
    label = _wdog._get_thread_label(db, thread_id) if thread_id else agent_id[:8]

    # Fix B (2026-07-07 completed-agents-leak): NEVER re-dispatch already-landed
    # work — release the stale agent + mark done, no replacement (see reap_done).
    if thread_id:
        from juggle_watchdog_reap_done import release_if_work_landed
        if release_if_work_landed(db, mgr, live, thread_id, label, session_id):
            return

    # Never-tasked agent: silently decommission — no snapshot, no thread=failed,
    # no action item.  The orchestrator hadn't sent work yet so this is not a
    # real failure.  Guard: skip decommission during cold-boot grace period so
    # freshly-spawned agents aren't reaped before Claude UI has rendered.
    if not last_task:
        try:
            from juggle_settings import get_settings as _get_settings
            _grace = float(_get_settings().get("agent_boot_grace_secs", _wdog._BOOT_GRACE_SECS))
        except Exception:
            _grace = _wdog._BOOT_GRACE_SECS
        _age = _wdog._get_agent_age_secs(live)
        if _age < _grace:
            _log.info(
                "Watchdog: agent %s never-tasked but young (age=%.0fs < grace=%.0fs) — skipping",
                agent_id[:8], _age, _grace,
            )
            return
        _log.info(
            "Watchdog: agent %s never tasked (age=%.0fs >= grace=%.0fs) — silently decommissioning",
            agent_id[:8], _age, _grace,
        )
        try:
            mgr.kill_pane(live["pane_id"])
        except Exception:
            pass
        db.delete_agent(agent_id)
        db.add_watchdog_event(
            agent_id=agent_id,
            thread_id=thread_id,
            event_type="decommissioned_untasked",
            snapshot_path=None,
        )
        return

    # Liveness recheck (Fix 3): re-capture pane content before committing to recovery.
    # If the hash changed since the watchdog's original observation, the agent is still
    # working (e.g. running a long test suite) — abort to avoid duplicate dispatch.
    try:
        _recheck_content = mgr.capture_pane(live["pane_id"])
        if _recheck_content is not None:
            _initial_hash = _wdog._hash_tail(_wdog._strip_ansi(pane_content))
            _recheck_hash = _wdog._hash_tail(_wdog._strip_ansi(_recheck_content))
            if _initial_hash != _recheck_hash:
                _log.info(
                    "Watchdog: recovery aborted for %s — pane hash changed (agent still active)",
                    agent_id[:8],
                )
                return
    except Exception as _exc:
        _log.debug("Watchdog: liveness recheck failed for %s: %s — proceeding", agent_id[:8], _exc)

    snap_path = _wdog.write_recovery_snapshot(agent_id, pane_content, recovery_dir)
    _log.info("Watchdog: recovery snapshot saved to %s", snap_path)

    if thread_id:
        # P8 Task 4.2: update_thread mirrors the conversation node get_thread reads.
        db.update_thread(
            thread_id,
            last_dispatched_task=last_task,
            last_dispatched_role=role,
            last_dispatched_model=model,
        )

    # Kill pane (best-effort) then delete agent from DB directly
    try:
        mgr.kill_pane(live["pane_id"])
    except Exception:
        pass
    db.delete_agent(agent_id)
    try:  # Ledger: a reaped agent's open run must not linger as 'dispatched'.
        db.fail_open_runs(thread_id=thread_id, agent_id=agent_id)
    except Exception:
        _log.warning("Watchdog: ledger fail_open_runs failed for %s", agent_id[:8])

    if thread_id:
        db.update_thread(thread_id, status="failed")

    if live.get("watchdog_retried", 0) >= 1:
        if thread_id:
            db.add_action_item(
                thread_id=thread_id,
                message=(
                    f"[RQ] [{label}] {role} agent failed 2× (auto-recovery exhausted). "
                    f"Decide: re-dispatch / abandon / investigate. "
                    f"Cause: stalled/crashed again after watchdog retry."
                ),
                type_="failure",
                priority="high",
            )
        if thread_id:
            # Agent death must reach the graph (DA round-2 MAJOR-1,
            # 2026-06-10): bound task → failed-exec + dependents blocked.
            try:
                from juggle_cmd_agents_graph import fail_graph_task

                fail_graph_task(
                    db, thread_id, session_id,
                    reason="watchdog auto-recovery exhausted (failed 2x)",
                )
            except Exception:
                _log.exception(
                    "Watchdog: graph fail-marking failed for thread %s",
                    thread_id[:8],
                )
        db.add_watchdog_event(
            agent_id=agent_id,
            thread_id=thread_id,
            event_type="retry_blocked",
            snapshot_path=str(snap_path),
        )
        return

    if thread_id:
        db.emit_event(
            thread_id=thread_id, session_id=session_id, kind=_ek.WATCHDOG_RECOVERY,
            message=(f"[Watchdog] [{label}] {role} stalled/crashed — auto-retrying "
                     f"(recovery snapshot: {snap_path.name})"),
        )

    new_agent = mgr.spawn_agent(db, role=role, model=model)
    new_agent_id = new_agent["id"]
    new_pane_id = new_agent["pane_id"]

    # Fix 3b: if thread was closed DURING spawn (original agent finished just-in-time),
    # release the recovery agent immediately — update_thread below would otherwise
    # overwrite the "closed" status, hiding the completion.
    if thread_id:
        _thread_post_spawn = db.get_thread(thread_id)
        if _thread_post_spawn and _thread_post_spawn.get("state") == "done":
            _log.info(
                "Watchdog: recovery agent %s released — thread %s closed during spawn window",
                new_agent_id[:8], thread_id[:8],
            )
            db.update_agent(new_agent_id, status="idle", assigned_thread=None)
            return

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(
        new_agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_active=now,
        busy_since=now,
        watchdog_retried=1,
        last_task=last_task,
    )
    if thread_id:
        db.set_conversation_background(thread_id)

    try:
        pane_hash = mgr.send_task(new_pane_id, last_task)
    except RuntimeError as exc:
        _log.error(
            "Watchdog: [RECOVERY-COLD-START-FAILED] send_task raised for agent %s: %s",
            new_agent_id[:8],
            exc,
        )
        # Rollback: thread is not actually being recovered if the agent can't start.
        if thread_id:
            db.update_thread(thread_id, status="failed")
        db.delete_agent(new_agent_id)
        try:
            mgr.kill_pane(new_pane_id)
        except Exception:
            pass
        cascade_status = _wdog._record_cold_start_failure(thread_id)
        if thread_id and cascade_status != "cascade_suppress":
            if cascade_status == "cascade_fire":
                db.add_action_item(
                    thread_id=thread_id,
                    message=(
                        f"🛑 [{label}] WATCHDOG-CASCADE-DETECTED — "
                        f"≥{_wdog._CASCADE_THRESHOLD} cold-start failures within "
                        f"{int(_wdog._CASCADE_WINDOW_SECS // 60)} min. "
                        f"Check spawn config (tmux truncation?). "
                        f"Latest: {exc}"
                    ),
                    type_="failure",
                    priority="high",
                )
            else:
                db.add_action_item(
                    thread_id=thread_id,
                    message=(
                        f"🚨 [{label}] [RECOVERY-COLD-START-FAILED] recovery send_task "
                        f"raised: {exc}. New agent {new_agent_id[:8]} spawned but task "
                        f"not sent — re-dispatch manually."
                    ),
                    type_="failure",
                    priority="high",
                )
        db.add_watchdog_event(
            agent_id=new_agent_id,
            thread_id=thread_id,
            event_type="cold_start_failed",
            snapshot_path=str(snap_path),
        )
        return

    from juggle_dispatch_stamp import record_dispatch  # 2026-07-13: see its docstring
    record_dispatch(db, new_agent_id, last_task=last_task, pane_hash=pane_hash)

    if thread_id:
        db.emit_event(
            thread_id=thread_id, session_id=session_id, kind=_ek.WATCHDOG_RECOVERY,
            message=(f"[Watchdog] [{label}] {role} agent auto-re-dispatched to "
                     f"{new_agent_id[:8]} after stall"),
        )

    db.add_watchdog_event(
        agent_id=agent_id,
        thread_id=thread_id,
        event_type="recovered",
        snapshot_path=str(snap_path),
    )
    # Clear cascade state and dismiss any open cold-start failure items for this thread
    _wdog._clear_cold_start_failures(thread_id)
    if thread_id:
        for item in db.get_open_action_items():
            if item.get("thread_id") == thread_id and "COLD-START-FAILED" in item.get(
                "message", ""
            ):
                db.dismiss_action_item(item["id"])
    _log.info(
        "Watchdog: re-dispatched %s → %s for thread %s",
        agent_id[:8],
        new_agent_id[:8],
        (thread_id or "")[:8],
    )
