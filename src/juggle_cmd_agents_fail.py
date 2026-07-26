"""juggle_cmd_agents_fail — `juggle agent fail` handler.

Owns: cmd_fail_agent (transient -> leave running for retry; persistent ->
action item + close + graph fail).
Must not own: completion (juggle_cmd_agents_complete), spawn/release lifecycle,
worktree helpers, classifiers (juggle_cmd_agents_common).

EXTRACTED MECHANICALLY from juggle_cmd_agents_complete (2026-07-25, architecture
LOC gate): that module sat at exactly 300/300 and the notebook completion hook
needed headroom. Body byte-identical; juggle_cmd_agents_complete re-exports the
name so every existing import and test keeps working unchanged.
"""

import sys

import juggle_cmd_agents_common as _com
from dbops import event_kinds as _ek


def cmd_fail_agent(args):
    """Route agent failure: transient → leave running for retry; persistent → action_item + close."""
    import juggle_cli_common as _common
    from juggle_spool_cli_common import spool_event_if_agent

    _resolved_tid = _common.resolve_thread_id_for_spool(args.thread_id)
    _fa_args = dict(
        thread_id=_resolved_tid, error=args.error, failure_type=getattr(args, "failure_type", None),
        max_retries=getattr(args, "max_retries", 0), recovery_dispatched=getattr(args, "recovery_dispatched", False),
    )
    if spool_event_if_agent("agent_fail", _fa_args):
        print(f"Agent failure for Topic {args.thread_id} → spooled.")
        return

    db = _common.get_db()
    thread_uuid = _common._resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    # Explicit classification wins; else auto-classify
    ft = getattr(args, "failure_type", None)
    if ft not in ("transient", "persistent"):
        ft = _com._classify_failure(args.error or "")

    label = thread.get("user_label") or thread.get("label") or args.thread_id

    if ft == "transient":
        db.touch_last_active(thread_uuid)
        max_retries = getattr(args, "max_retries", 0)
        print(
            f"Transient failure on Topic {label}; thread stays 'running' "
            f"(max_retries={max_retries}). Error: {args.error}"
        )
        return

    # Fetch session_id for notifications
    with db._connect() as conn:
        srow = conn.execute(
            "SELECT value FROM session WHERE key = 'session_id'"
        ).fetchone()
    session_id = srow["value"] if srow else ""

    # Dismiss pre-existing action items for this thread
    open_items = db.get_open_action_items()
    for item in open_items:
        if item.get("thread_id") == thread_uuid:
            db.dismiss_action_item(item["id"])

    # Release assigned agent
    agent = db.get_agent_by_thread(thread_uuid)
    if agent:
        db.update_agent(agent["id"], status="idle", assigned_thread=None)

    recovery = getattr(args, "recovery_dispatched", False)
    if recovery:
        db.emit_event(
            thread_id=thread_uuid, message=f"⟳ [{label}] Recovery dispatched — {args.error}",
            session_id=session_id, kind=_ek.AGENT_RECOVERY_DISPATCHED,
        )
        _com._set_thread_status_unless_archived(db, thread, thread_uuid, "running")
        print(
            f"Recovery dispatched for Topic {label}; notification logged, thread stays running."
        )
    else:
        db.add_action_item(
            thread_id=thread_uuid,
            message=f"⚠️ Agent failure — {args.error}. No recovery possible.",
            type_="failure",
            priority="high",
        )
        db.emit_event(
            thread_id=thread_uuid, message=f"✗ [{label}] Unrecoverable — {args.error}",
            session_id=session_id, kind=_ek.AGENT_FAILURE,
        )
        _com._set_thread_status_unless_archived(db, thread, thread_uuid, "closed")
        # Agent death must reach the graph (DA round-2 MAJOR-1, 2026-06-10):
        # bound task → failed-exec + dependents blocked + HIGH action item.
        from juggle_cmd_agents_graph import fail_graph_task

        fail_graph_task(
            db, thread_uuid, session_id,
            reason=f"unrecoverable agent failure: {args.error}",
        )
        print(
            f"Unrecoverable failure on Topic {label}; HIGH action item created, thread → closed."
        )
