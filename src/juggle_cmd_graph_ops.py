"""
juggle_cmd_graph_ops — live graph *mutator* command handlers.

Extracted from juggle_cmd_graph (2026-07-03, Phase3) so the command group stays
under the 300-LOC architecture gate before new graph commands are added. Owns
the three graph-editing handlers: add-task, reconcile, mark-task.

Must not own: parser wiring (juggle_graph_cli_parsers), whole-spec load
(juggle_graph_load), PR-mode refusal policy / _git_root (juggle_cmd_graph —
shared with load), task state semantics (dbops.db_graph / db_topics), or the
single-task upsert (juggle_graph_add / juggle_graph_upsert).

get_db and pr_mode_refusal are resolved through the juggle_cmd_graph module at
call time (``import juggle_cmd_graph as cg`` INSIDE each handler — lazy to avoid
the import cycle, since juggle_cmd_graph imports this module to re-export the
handlers) so the test-suite monkeypatches (``cg.get_db`` / ``cg._git_root``)
keep taking effect after the move.
"""

from __future__ import annotations

import sys

from dbops import db_graph, db_topics

# Default priority for a 'fix-'-prefixed add-task id lacking an explicit
# --priority (T-fix-priority-dispatch-ordering): outranks 0-default feature tasks.
FIX_DEFAULT_PRIORITY = 100


def _csv(value) -> list[str]:
    """Split a comma-separated CLI arg into a clean id list (``None`` → [])."""
    if not value:
        return []
    return [tok.strip() for tok in value.split(",") if tok.strip()]


def cmd_graph_add_task(args):
    """Inject one new task into an EXISTING project graph mid-execution.

    Validated, atomic, guarded upsert via juggle_graph_upsert.add_task — refuses
    (nonzero exit, graph unchanged) on unknown deps, a cycle, an empty prompt,
    or touching a protected task. Supports args-or-stdin for --prompt so a long
    dispatch prompt can be piped.
    """
    import json

    import juggle_cmd_graph as cg  # lazy: break the re-export import cycle
    from juggle_graph_add import (
        AddTaskError, add_task, record_surfacing_conversation,
        resolve_dispatch_topic,
    )

    db = cg.get_db(getattr(args, "db_path", None), init=True)
    if not db.get_project(args.project):
        print(f"Error: project {args.project!r} not found.", file=sys.stderr)
        sys.exit(1)

    refusal = cg.pr_mode_refusal()
    if refusal:
        print(f"Error: {refusal}", file=sys.stderr)
        sys.exit(1)

    prompt = args.prompt
    if prompt is None or prompt == "-":
        prompt = sys.stdin.read()
    prompt = (prompt or "").strip()

    # DEFAULT-DISPATCHABLE (2026-06-30 orphan-task dispatch gap): every new task
    # MUST be owned by a graph-topic so graph_tick can dispatch it. A missing
    # --topic, or one pointing at a non-graph-topic node (e.g. a conversation),
    # auto-creates a synthetic 'T-<id>' graph-topic home — no code path leaves a
    # parentless orphan. add_task builds topic+task+FK in one transaction.
    topic_id, auto_topic = resolve_dispatch_topic(
        db, args.project, args.id, getattr(args, "topic", None)
    )
    # Explicit --priority wins; else a 'fix-' id defaults high (fix-first dispatch).
    priority = getattr(args, "priority", None)
    if priority is None:
        priority = FIX_DEFAULT_PRIORITY if args.id.startswith("fix-") else 0
    try:
        result = add_task(
            db, args.project,
            task_id=args.id, title=args.title, prompt=prompt,
            deps=_csv(args.deps), required_by=_csv(args.required_by),
            verify_cmd=None,
            topic_id=topic_id, auto_create_topic=auto_topic,
            priority=priority,
            plan_path=getattr(args, "plan", None),
            spec_path=getattr(args, "spec", None),
        )
    except AddTaskError as e:
        if getattr(args, "json_out", False):
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"add-task REFUSED — graph unchanged: {e}", file=sys.stderr)
        sys.exit(1)

    # Dedup defect F (2026-07-01): a --topic pointing at an existing conversation
    # becomes the synthetic topic's surfacing/dispatch thread, so graph_tick reuses
    # it instead of spawning a second "[T-<id>]" mirror row.
    if auto_topic:
        record_surfacing_conversation(db, topic_id, getattr(args, "topic", None))

    if getattr(args, "json_out", False):
        print(json.dumps({"ok": True, **result}))
        return
    if result.get("topic_reopened"):  # fr-vf-rails silent-wedge fix (2026-07-03)
        print(f"topic {topic_id!r} reopened by add-task {result['task_id']!r}")
    changed = result["downstream_changed"]
    tail = ""
    if changed:
        tail = " downstream: " + ", ".join(
            f"{c['id']} {c['from']}→{c['to']}" for c in changed
        )
    print(
        f"Added task {result['task_id']!r} to project {args.project} "
        f"(state: {result['state']}).{tail}"
    )


def cmd_graph_reconcile(args):
    """`juggle graph reconcile <project>` — re-derive topic states from tasks."""
    import json as _json

    import juggle_cmd_graph as cg  # lazy: break the re-export import cycle

    db = cg.get_db(getattr(args, "db_path", None), init=True)
    if not db.get_project(args.project):
        print(f"Error: project {args.project!r} not found.", file=sys.stderr)
        sys.exit(1)

    result = db_topics.reconcile_project_topics(db, args.project)

    if getattr(args, "json_out", False):
        print(_json.dumps(result))
        return

    for topic_id, info in result.items():
        before, after = info["before"], info["after"]
        if before != after:
            print(f"  {topic_id}: {before} → {after}")
        else:
            print(f"  {topic_id}: {before} (unchanged)")


LEARNINGS_MAX = 300


def cmd_graph_learn(args):
    """`juggle graph learn <node-id> "<text>"` — upsert a node's cross-session
    learnings: non-obvious gotchas / constraints / decision-why worth reading by
    the NEXT agent (NOT status/what-was-done — the handoff/completion summary owns
    that). Callable any time mid-task; overwrite-on-rewrite (spec §2).

    Fail-loud + nonzero exit on: empty/whitespace text, >300 chars
    (verbatim length — NEVER truncated), or unknown node. Text stored verbatim."""
    import juggle_cmd_graph as cg  # lazy: break the re-export import cycle
    from juggle_spool_cli_common import spool_event_if_agent

    text = args.text if getattr(args, "text", None) is not None else ""
    # Validate content BEFORE any spool/DB touch so agents get immediate fail-loud
    # feedback rather than a deferred dead-letter at replay time.
    if not text.strip():
        print("Error: learnings text is empty — nothing to record.", file=sys.stderr)
        sys.exit(1)
    if len(text) > LEARNINGS_MAX:
        print(
            f"learnings too long ({len(text)}/{LEARNINGS_MAX}) — compress: "
            "keep decision + why, drop narrative",
            file=sys.stderr,
        )
        sys.exit(1)

    # Agent context: spool the write for the single-writer watchdog broker (agent
    # processes must not open the shared DB read-write) — mirrors mark-task.
    if spool_event_if_agent("graph_learn", {"node_id": args.node_id, "text": text}):
        print(f"learnings for {args.node_id} → spooled")
        return

    db = cg.get_db(getattr(args, "db_path", None), init=True)
    if not db_graph.get_task(db, args.node_id):
        print(f"Error: node {args.node_id!r} not found.", file=sys.stderr)
        sys.exit(1)
    db_graph.set_learnings(db, args.node_id, text)
    print(f"learnings for {args.node_id} recorded ({len(text)}/{LEARNINGS_MAX}).")


def cmd_graph_mark_task(args):
    """`juggle graph mark-task <task-id> [--fail] [--handoff '…']` — the topic
    agent's per-task completion (R9 hybrid). Maps onto the EXISTING task machine
    via mark_completion(integrate_ok=True, verify_ok=not --fail): task 'verified'
    = committed-in-topic-worktree + verify_cmd green — verified-means-MERGED
    holds at TOPIC level only (spec §2.3)."""
    import juggle_cmd_graph as cg  # lazy: break the re-export import cycle
    from juggle_spool_cli_common import spool_event_if_agent
    _mt_args = {"task_id": args.task_id, "fail": getattr(args, "fail", False),
                "handoff": getattr(args, "handoff", None)}
    if spool_event_if_agent("graph_mark_task", _mt_args):
        print(f"task {args.task_id} → spooled")
        return
    db = cg.get_db(getattr(args, "db_path", None), init=True)
    task = db_graph.get_task(db, args.task_id)
    if not task:
        print(f"Error: task {args.task_id!r} not found.", file=sys.stderr)
        sys.exit(1)
    try:
        state = db_graph.mark_completion(
            db, args.task_id, integrate_ok=True,
            verify_ok=not getattr(args, "fail", False),
            handoff=getattr(args, "handoff", None),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"task {args.task_id} → {state}")
    topic_id = db_graph.get_task(db, args.task_id)["topic_id"]
    if topic_id:
        db_topics.reconcile_topic_state(db, topic_id)
