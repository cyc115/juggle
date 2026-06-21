#!/usr/bin/env python3
"""Juggle CLI — Agent pool management commands (facade + action-item/watchdog ctl).

Owns: cmd_request_action, cmd_ack_action, cmd_list_actions, cmd_notify,
      cmd_set_watchdog, cmd_stop_watchdog — plus re-exports of the full
      agent-command family so `from juggle_cmd_agents import X` keeps working.

The family is split by domain seam:
  juggle_cmd_agents_common    — shared symbols + pure classifiers (test patch surface)
  juggle_cmd_agents_worktree  — _create_worktree / _finalize_worktree
  juggle_cmd_agents_pool      — spawn / list / check
  juggle_cmd_agents_lifecycle — get / release / decommission
  juggle_cmd_agents_complete  — complete / fail
  juggle_cmd_agents_tasks     — send-task / send-message

Agent completion protocol (embed in all dispatched prompts):
  When finished, call EXACTLY ONE of:
  1. Success:  juggle complete-agent <hex6> "<result>"
  2. Action needed: juggle request-action <hex6> "<what>" --type manual_step --priority high
  3. Failure:  juggle fail-agent <hex6> "<error>"
"""

import sys
from pathlib import Path

# Re-exported shared symbols (single patch surface: juggle_cmd_agents_common)
from juggle_cmd_agents_common import (  # noqa: F401
    _AGENT_TTL_SECS,
    _COMPLETE_PATTERNS,
    _DRAFT_PATTERNS,
    _PERSISTENT_HINTS,
    _PLAN_PATTERNS,
    _TRANSIENT_PATTERNS,
    SRC_DIR,
    UNIVERSAL_PREAMBLE,
    JuggleTmuxManager,
    _classify_failure,
    _get_hindsight_client,
    _get_settings,
    _last_sentences,
    _looks_complete,
    _matches_draft,
    _matches_plan,
    _resolve_thread,
    get_adapter,
    get_db,
)
import juggle_cmd_integrate  # noqa: F401 — re-export (tests patch via _common)
from juggle_cmd_agents_worktree import (  # noqa: F401
    _create_worktree,
    _finalize_worktree,
)
from juggle_cmd_agents_pool import (  # noqa: F401
    cmd_check_agents,
    cmd_list_agents,
    cmd_spawn_agent,
)
from juggle_cmd_agents_lifecycle import (  # noqa: F401
    cmd_decommission_agent,
    cmd_get_agent,
    cmd_release_agent,
)
from juggle_cmd_agents_complete import (  # noqa: F401
    cmd_complete_agent,
    cmd_fail_agent,
)
from juggle_cmd_agents_tasks import (  # noqa: F401
    cmd_send_message,
    cmd_send_task,
)


def cmd_request_action(args):
    """Create an action_items row tied to a thread. Thread stays in current state;
    only last_active_at is touched."""
    import juggle_cli_common as _common

    db = _common.get_db()
    thread_uuid = _common._resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    priority = getattr(args, "priority", None) or "normal"
    type_ = getattr(args, "type", None) or "manual_step"
    if priority not in ("low", "normal", "high"):
        print(f"Error: priority must be low/normal/high, got {priority!r}")
        sys.exit(1)
    if type_ not in ("question", "manual_step", "decision", "failure"):
        print(
            f"Error: type must be question/manual_step/decision/failure, got {type_!r}"
        )
        sys.exit(1)
    aid = db.add_action_item(
        thread_id=thread_uuid,
        message=args.message,
        type_=type_,
        priority=priority,
    )
    db.touch_last_active(thread_uuid)

    agent = db.get_agent_by_thread(thread_uuid)
    if agent:
        db.update_agent(agent["id"], status="idle", assigned_thread=None)

    label = thread.get("user_label") or thread.get("label") or args.thread_id
    print(f"Action item #{aid} logged for Topic {label} (priority={priority}).")


def cmd_ack_action(args):
    """Dismiss an action item by id."""
    import juggle_cli_common as _common

    db = _common.get_db()
    try:
        action_id = int(args.action_id)
    except ValueError:
        print(f"Error: expected a numeric action id, got {args.action_id!r}.")
        sys.exit(1)
    db.dismiss_action_item(action_id)
    print(f"Action item #{action_id} dismissed.")


def cmd_list_actions(_):
    """Print open action items, newest high-priority first."""
    import juggle_cli_common as _common

    db = _common.get_db()
    items = db.get_open_action_items()
    if not items:
        print("No open action items.")
        return
    for it in items:
        thread_suffix = ""
        if it.get("thread_id"):
            t = db.get_thread(it["thread_id"])
            if t:
                lbl = t.get("user_label") or it["thread_id"][:6]
                thread_suffix = f" (thread [{lbl}])"
        print(
            f"⚡ [{it['id']}] {it['priority'].upper():6} {it['message']}{thread_suffix}"
        )


def cmd_notify(args):
    """Insert a notifications_v2 row for the given thread."""
    import juggle_cli_common as _common

    db = _common.get_db()
    thread_uuid = _common._resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    with db._connect() as conn:
        srow = conn.execute(
            "SELECT value FROM session WHERE key = 'session_id'"
        ).fetchone()
    session_id = srow["value"] if srow else ""
    db.add_notification_v2(
        thread_id=thread_uuid, message=args.message, session_id=session_id
    )
    db.touch_last_active(thread_uuid)
    label = thread.get("user_label") or thread_uuid[:6]
    print(f"Notification logged for Topic {label}.")


def cmd_set_watchdog(args):
    db = get_db()
    agent = db.get_agent(args.agent_id)
    if agent is None:
        print(f"Error: Agent {args.agent_id} not found.")
        sys.exit(1)
    if args.value == "off":
        db.update_agent(args.agent_id, watchdog_threshold_minutes=-1)
        print(f"Watchdog disabled for agent {args.agent_id[:8]}.")
    else:
        try:
            minutes = int(args.value)
            if minutes <= 0:
                raise ValueError
        except ValueError:
            print(
                f"Error: value must be a positive integer or 'off', got {args.value!r}"
            )
            sys.exit(1)
        db.update_agent(args.agent_id, watchdog_threshold_minutes=minutes)
        print(f"Watchdog threshold for agent {args.agent_id[:8]} set to {minutes} min.")


def cmd_stop_watchdog(args):
    """Terminate EVERY running watchdog process — a freeze must freeze them all.

    2026-06-16 incident: stopping only the recorded PID let a rogue watchdog
    (started from a worktree) survive and keep ticking against the prod DB.

    --freeze (2026-06-20 incident): also set the freeze sentinel so the cockpit's
    15s ensure cannot respawn the daemon — the defect-protocol freeze can finally
    hold. An explicit `start` / W / R hotkey clears it.
    """
    from juggle_settings import get_settings
    from juggle_watchdog_singleton import freeze_watchdog, terminate_all_watchdogs

    # Best-effort cleanup of any recorded pidfiles (session-scoped + legacy).
    config_dir = Path(get_settings()["paths"]["config_dir"])
    for pid_file in [config_dir / "watchdog.pid", *config_dir.glob("watchdog-*.pid")]:
        pid_file.unlink(missing_ok=True)

    if getattr(args, "freeze", False):
        from dbops.schema import _resolve_db_path
        freeze_watchdog(str(_resolve_db_path()))

    killed = terminate_all_watchdogs()
    frozen_note = " + frozen (no respawn until start)" if getattr(args, "freeze", False) else ""
    if killed:
        print(f"Watchdog stopped ({len(killed)} process(es): "
              f"{', '.join(str(p) for p in killed)}){frozen_note}.")
    else:
        print(f"Watchdog is not running{frozen_note}.")
