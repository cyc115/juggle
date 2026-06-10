"""
juggle_cmd_agents_complete — Agent completion and failure handlers.

Owns: cmd_complete_agent, cmd_fail_agent.
Must not own: spawn/get/release lifecycle, task dispatch, worktree helpers
(juggle_cmd_agents_worktree), classifiers (juggle_cmd_agents_common).

Shared symbols are accessed through juggle_cmd_agents_common (_com) at call
time so test monkeypatches on _com.<symbol> take effect.
"""

import json
import sys
import threading
from datetime import datetime, timezone

import juggle_cmd_agents_common as _com


def cmd_complete_agent(args):
    """Mark agent complete: thread → closed, create notifications_v2 row,
    convert any open_questions to action_items."""
    import juggle_cli_common as _common
    db = _common.get_db()
    thread_uuid = _common._resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    # Finalize worktree BEFORE closing the thread.
    # Route through _run_integrate (rebase-aware) when worktree fields are present;
    # fall back to bare _finalize_worktree for pre-migration threads.
    if thread.get("worktree_path") and thread.get("worktree_branch") and thread.get("main_repo_path"):
        ft_success, ft_msg = _com.juggle_cmd_integrate._run_integrate(thread, db)
    else:
        ft_success, ft_msg = _com._finalize_worktree(thread)

    if not ft_success:
        db.add_action_item(
            thread_id=thread_uuid,
            message=f"⚠️ Worktree finalization failed: {ft_msg}",
            type_="manual_step",
            priority="high",
        )
        args.result_summary = f"{args.result_summary} [WARNING: worktree not finalized — {ft_msg}]"

    # Current session id
    with db._connect() as conn:
        srow = conn.execute(
            "SELECT value FROM session WHERE key = 'session_id'"
        ).fetchone()
    session_id = srow["value"] if srow else ""

    # Snapshot pre-existing open action items to dismiss after close
    items_to_dismiss = [
        item["id"]
        for item in db.get_open_action_items()
        if item.get("thread_id") == thread_uuid
    ]

    # 1. Convert any open_questions to action_items
    oq_raw = thread.get("open_questions") or "[]"
    try:
        open_questions = (
            json.loads(oq_raw) if isinstance(oq_raw, str) else (oq_raw or [])
        )
    except (json.JSONDecodeError, ValueError):
        open_questions = []
    for q in open_questions:
        if isinstance(q, dict):
            text = q.get("text") or q.get("question") or q.get("q") or str(q)
        else:
            text = str(q)
        db.add_action_item(
            thread_id=thread_uuid,
            message=text,
            type_="question",
            priority="normal",
        )
    if open_questions:
        db.update_thread(thread_uuid, open_questions="[]")

    # 2. Store the agent result as an assistant message
    if args.result_summary:
        db.add_message(thread_uuid, role="assistant", content=args.result_summary)

    # 3. Auto-generate summary if the thread has none yet
    if not (thread.get("summary") or "").strip():
        exchange = db.get_last_exchange(thread_uuid)
        raw_last_user = exchange.get("last_user") or ""
        is_junk = (
            raw_last_user.startswith("<task-notification")
            or "task-id" in raw_last_user
            or raw_last_user.strip().startswith("/")
        )
        if not is_junk:
            last_q = _com._last_sentences(raw_last_user, max_chars=80)
            last_a = _com._last_sentences(exchange.get("last_assistant") or "", max_chars=80)
            if last_q or last_a:
                auto_summary = (
                    f"{last_q} -> {last_a}"
                    if (last_q and last_a)
                    else (last_q or last_a)
                )
                db.update_thread(thread_uuid, summary=auto_summary)

    # 4. Transition thread to closed
    db.set_thread_status(thread_uuid, "closed")

    # Resolve agent before step 5 (needed for role check below)
    agent = db.get_agent_by_thread(thread_uuid)
    if agent:
        busy_since = agent.get("busy_since")
        if busy_since:
            try:
                busy_dt = datetime.fromisoformat(busy_since.replace("Z", "+00:00"))
                if busy_dt.tzinfo is None:
                    busy_dt = busy_dt.replace(tzinfo=timezone.utc)
                duration = (datetime.now(timezone.utc) - busy_dt).total_seconds()
                db.insert_agent_completion(role=agent["role"], duration_secs=duration)
            except (ValueError, TypeError):
                pass
        db.update_agent(agent["id"], status="idle", assigned_thread=None)

    # 5. Create notification row (informational, session TTL)
    title = thread.get("title") or thread.get("topic") or "thread"
    db.add_notification_v2(
        thread_id=thread_uuid,
        message=f"{title}: {args.result_summary}",
        session_id=session_id,
    )

    # 6a. Role-based action items
    role = (agent.get("role") if agent else None) or getattr(args, "role", None)
    if role == "researcher" and open_questions:
        db.add_action_item(
            thread_id=thread_uuid,
            message=f"Review: {args.result_summary}",
            type_="review",
            priority="normal",
        )
    elif role == "planner":
        db.add_action_item(
            thread_id=thread_uuid,
            message=f"Review plan before dispatching coder: {args.result_summary}",
            type_="decision",
            priority="normal",
        )
    elif role not in ("researcher", "planner"):
        summary = args.result_summary or ""
        if _com._matches_plan(summary):
            db.add_action_item(
                thread_id=thread_uuid,
                message=f"Review before dispatching coder: {args.result_summary}",
                type_="decision",
                priority="normal",
            )
        elif _com._matches_draft(summary) and not _com._looks_complete(summary):
            db.add_action_item(
                thread_id=thread_uuid,
                message=f"Review/iterate: {args.result_summary}",
                type_="manual_step",
                priority="normal",
            )

    # 6b. Optional retain text → Hindsight
    retain_text = getattr(args, "retain_text", None)
    if retain_text:

        def _do_retain(text, topic):
            client = _com._get_hindsight_client()
            if client:
                try:
                    client.retain(f"[{topic}] {text}", context="learnings")
                except Exception:
                    pass

        threading.Thread(
            target=_do_retain,
            args=(retain_text, thread.get("topic", "")),
            daemon=True,
        ).start()

    # Auto-dismiss pre-existing action items (not ones just created from open_questions)
    for item_id in items_to_dismiss:
        db.dismiss_action_item(item_id)

    label = thread.get("user_label") or thread.get("label") or args.thread_id
    print(f"Agent complete for Topic {label} → closed. Notification logged.")


def cmd_fail_agent(args):
    """Route agent failure: transient → leave running for retry; persistent → action_item + close."""
    import juggle_cli_common as _common

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
        db.add_notification_v2(
            thread_id=thread_uuid,
            message=f"⟳ [{label}] Recovery dispatched — {args.error}",
            session_id=session_id,
        )
        db.set_thread_status(thread_uuid, "running")
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
        db.add_notification_v2(
            thread_id=thread_uuid,
            message=f"✗ [{label}] Unrecoverable — {args.error}",
            session_id=session_id,
        )
        db.set_thread_status(thread_uuid, "closed")
        print(
            f"Unrecoverable failure on Topic {label}; HIGH action item created, thread → closed."
        )
