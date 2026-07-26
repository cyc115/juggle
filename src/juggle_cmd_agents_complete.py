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

import juggle_cmd_agents_common as _com
from dbops import event_kinds as _ek

# cmd_fail_agent moved to juggle_cmd_agents_fail (LOC gate, 2026-07-25).
# Re-exported: juggle_spool_apply._dispatch and tests import it from HERE.
from juggle_cmd_agents_fail import cmd_fail_agent  # noqa: F401


def cmd_complete_agent(args):
    """Mark agent complete: thread → closed, create notifications_v2 row,
    convert any open_questions to action_items."""
    import juggle_cli_common as _common
    from juggle_spool_cli_common import spool_event_if_agent

    # Resolve thread_id BEFORE spooling (DA Resolution #6): avoids a freed/reassigned label misapplying a replayed event.
    _resolved_tid = _common.resolve_thread_id_for_spool(args.thread_id)
    _ca_args = dict(
        thread_id=_resolved_tid, result_summary=args.result_summary,
        retain_text=getattr(args, "retain_text", None), open_questions=getattr(args, "open_questions", None),
        handoff=getattr(args, "handoff", None), role=getattr(args, "role", None),
    )
    if spool_event_if_agent("agent_complete", _ca_args):
        print(f"Agent complete for Topic {args.thread_id} → spooled.")
        return

    db = _common.get_db()
    thread_uuid = _common._resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    # Output contract (DA M4): a graph task with dependents MUST hand off.
    # Enforced BEFORE any side effects — refusal leaves task + thread untouched.
    from juggle_cmd_agents_graph import close_adhoc_run, enforce_handoff_contract
    from juggle_cmd_agents_graph_topics import enforce_topic_gate

    enforce_handoff_contract(db, thread_uuid, getattr(args, "handoff", None))
    # R9/A10 topic gate: refuse a topic thread while any task is unmarked —
    # BEFORE integrate, so nothing is marked or merged on refusal.
    enforce_topic_gate(db, thread_uuid)

    # Fix 4 (2026-07-03 integrate-wedge): snapshot the pre-existing items to
    # dismiss BEFORE creating any new items — the finalization-failure item
    # below was previously swept into this snapshot and auto-dismissed,
    # silently swallowing a genuine integrate failure.
    items_to_dismiss = [
        item["id"]
        for item in db.get_open_action_items()
        if item.get("thread_id") == thread_uuid
    ]

    # Finalize worktree BEFORE closing the thread. A bound TOPIC hands the merge to
    # a DETACHED integrate (RC1 2026-07-04: NEVER run the gate inline in the
    # watchdog/spool process); legacy threads finalize inline. See the helper.
    from juggle_cmd_agents_graph_topics import finalize_or_detach_integrate
    ft_success, ft_msg, _detached_integrate = finalize_or_detach_integrate(
        db, thread, thread_uuid, getattr(args, "handoff", None))

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

    # Reap the pool agent (kept for the role check below). Fix 3 (2026-07-03
    # integrate-wedge Q3): reap INDEPENDENT of the live thread binding — via the
    # OPEN ledger run's recorded agent_id when get_agent_by_thread misses. MUST
    # run before close_adhoc_run, which closes the ad-hoc run it reads.
    from juggle_agent_reap import reap_completed_agent

    agent = reap_completed_agent(db, thread_uuid)

    close_adhoc_run(db, thread_uuid, args.result_summary)  # ledger (ad-hoc; graph→topic)

    # 3. Transition thread: close agent-owned ephemeral threads, but PRESERVE a
    #    user-facing feature topic an agent was wrongly bound to (2026-06-21:
    #    transient researcher 6238df03 closed feature Topic CQ on complete, had
    #    to unarchive). A thread with ≥1 real human-authored message is a feature
    #    topic; an agent-owned ephemeral thread has none — only automated chatter
    #    (task-notifications, '# Autonomous loop tick' headers) which the canonical
    #    classifier excludes. Code over prompts: don't rely on the orchestrator
    #    create-thread'ing a fresh ephemeral thread first.
    # R1 (2026-06-30 topic-graph-state-unify): the close/preserve decision is
    # extracted to juggle_topic_lifecycle.decide_thread_close — behavior-preserving.
    # None → leave untouched; "active" → un-hijack an in-flight wrongful bind
    # (gated on in-flight status so a duplicate completion never resurrects an
    # already-terminal feature topic — Codex review, 2026-06-21).
    from juggle_topic_lifecycle import decide_thread_close

    _new_status = decide_thread_close(db, thread, thread_uuid)
    preserve_feature_topic = _new_status != "closed"
    if _new_status is not None:
        db.set_thread_status(thread_uuid, _new_status)

    # 5. Create notification row (informational, session TTL)
    title = thread.get("title") or "thread"
    db.emit_event(
        thread_id=thread_uuid, message=f"{title}: {args.result_summary}",
        session_id=session_id, kind=_ek.AGENT_COMPLETE,
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

    # 6b. Graph-task marking (project autopilot Phase 1): map the integrate
    # outcome onto the bound task's state machine, store the handoff, and
    # notify newly-ready dependents. Notify ONLY — dispatch is watchdog-owned
    # (Phase 2); complete-agent never dispatches (DA B4/M1).
    # SKIP when detached (RC1): topic rests 'integrating'; the reconcile tick verdicts it.
    if not _detached_integrate:
        from juggle_cmd_agents_graph_topics import mark_graph_topic
        mark_graph_topic(
            db, thread_uuid, ft_success, getattr(args, "handoff", None), session_id,
        )

    # Fix 3 (ledger backstop): close this thread's ledger run by thread_id even
    # if an early-return skipped the normal closer (mark_graph_topic ValueError
    # / close_adhoc_run graph-skip). No-op when already closed.
    from juggle_agent_reap import close_thread_run_backstop

    close_thread_run_backstop(db, thread_uuid, args.result_summary)

    # Auto-dismiss pre-existing action items (not ones just created from open_questions)
    for item_id in items_to_dismiss:
        db.dismiss_action_item(item_id)

    label = thread.get("user_label") or thread.get("label") or args.thread_id
    if preserve_feature_topic:
        print(
            f"Agent complete for Topic {label} → feature topic preserved "
            f"(has user messages; not closed). Notification logged."
        )
    else:
        print(f"Agent complete for Topic {label} → closed. Notification logged.")
