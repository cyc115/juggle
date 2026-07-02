"""
juggle_cmd_graph — `juggle project-graph` command handlers (autopilot Phase 1).

Owns: the `project-graph load` command handler (orchestration + guarded upsert
loop) and the PR-mode refusal policy.
Must not own: pure spec parsing/validation or single-task upsert (extracted to
juggle_graph_upsert), task state semantics (dbops.db_graph), or dispatching.

Spec format (markdown), one `##` section per task:

    ## <task-id>: <Title>
    deps: dep1, dep2              (optional; `- deps:` also accepted)
    <remaining lines = dispatch prompt>
"""

from __future__ import annotations

import sys

from juggle_cli_common import get_db
# db_graph re-exported (used by add-task + the atomicity regression pin, which
# monkeypatches cg.db_graph — the same module object juggle_graph_load uses).
from dbops import db_graph, db_topics  # noqa: F401

# Re-exported for backward compatibility (tests + callers import these from here).
from juggle_graph_upsert import (  # noqa: F401
    MAX_TASKS,
    VERIFY_CMD_ALLOWLIST,
    find_cycle,
    lint_verify_cmd,
    parse_graph_spec,
    parse_topics_spec,
    validate_graph,
    validate_topics,
)
from juggle_graph_upsert import content_changed as _content_changed  # noqa: F401

# The load handler lives in juggle_graph_load (extracted 2026-06-11 for the LOC
# gate); re-exported so `juggle_cmd_graph.cmd_project_graph_load` stays valid for
# the parser registration and existing callers/tests.
from juggle_graph_load import cmd_project_graph_load  # noqa: F401

# Default priority for a 'fix-'-prefixed add-task id lacking an explicit
# --priority (T-fix-priority-dispatch-ordering): outranks 0-default feature tasks.
FIX_DEFAULT_PRIORITY = 100


def _is_synthetic_topic(topic_id: str) -> bool:
    """Synthetic single-task topics (migration-37 / flat-spec fallback) are
    named 'T-<task-id>' or 'T#<task-id>'. A project with ONLY synthetic topics
    is treated as a flat graph for add-task (topic optional)."""
    return topic_id.startswith("T-") or topic_id.startswith("T#")


def _git_root(cwd: str) -> str | None:
    """Toplevel of the git repo containing ``cwd``, or None."""
    import subprocess

    r = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def pr_mode_refusal(repo_path: str | None = None) -> str | None:
    """Refusal message when the target repo is push_mode='pr', else None.

    DA round-2 MAJOR-2 (2026-06-10): on PR-mode repos _run_integrate returns
    success after only pushing the branch — the task went 'verified' WITHOUT
    any merge, and dependents were hydrated with "already integrated into
    main" (false). Policy: autopilot (project-graph load / autopilot arm)
    refuses PR-mode repos until verified-means-merged holds for them.
    The target repo is the one the command runs in (worktrees are created
    from it on dispatch).
    """
    import os

    from juggle_settings import get_repo_config

    root = repo_path or _git_root(os.getcwd())
    if not root or get_repo_config(root)["push_mode"] != "pr":
        return None
    return (
        f"repo {root} is configured push_mode='pr' — integrate only pushes "
        "the branch for a PR (no merge into main), so autopilot would mark "
        "tasks 'verified' that are NOT in main and hydrate dependents with a "
        "false 'already integrated' claim. PR-mode repos are not supported "
        "by project autopilot: set push_mode to 'direct' or 'none', or drive "
        "this project without autopilot."
    )


def register_graph_parsers(subparsers) -> None:
    """Register the `graph` group: load (plan store) + live edits (add-task/
    reconcile/mark-task).

    P9 G2 folded the former `project-graph load` into `graph load`; legacy
    `project-graph …` resolves via the alias shim. Kept here (next to the handlers)
    so the graph CLI surface lives in one place.
    """
    # P9 G2: `project-graph load` is folded into the `graph` group as `graph load`
    # (legacy `project-graph load …` resolves via the alias shim, which maps
    # project-graph → graph). The standalone `project-graph` group is removed.
    p_g2 = subparsers.add_parser("graph", help="Live project task-graph edits")
    _g2s = p_g2.add_subparsers(dest="graph2_command", required=True)
    _g = _g2s.add_parser("load", help="Load/upsert a graph spec markdown file")
    _g.add_argument("file", help="Path to graph spec markdown")
    _g.add_argument("--project", required=True, help="Project id the graph belongs to")
    _g.set_defaults(func=cmd_project_graph_load)
    # 'add-node': deprecated hidden alias (baked into autopilot hook + CLAUDE.md).
    _an = _g2s.add_parser("add-task", aliases=["add-node"],
                          help="Inject one new task into an existing project graph")
    _an.add_argument("--project", required=True, help="Project id the graph belongs to")
    _an.add_argument("--id", required=True, help="Stable task id")
    _an.add_argument("--title", required=True, help="Task title")
    _an.add_argument(
        "--prompt", default=None,
        help="Dispatch prompt (omit or pass '-' to read from stdin)",
    )
    _an.add_argument(
        "--deps", default=None,
        help="Comma-separated EXISTING task ids this task depends on (upstream)",
    )
    _an.add_argument(
        "--required-by", dest="required_by", default=None,
        help="Comma-separated EXISTING task ids that gain a dep on this task",
    )
    _an.add_argument(
        "--topic", default=None,
        help="Owning topic id (REQUIRED when the project has real topics; "
        "omit on a flat project to auto-create a synthetic 'T-<task-id>' topic)",
    )
    _an.add_argument(
        "--priority", type=int, default=None,
        help="Dispatch priority (higher = first); default 0, 'fix-' ids default high",
    )
    _an.add_argument(
        "--json", dest="json_out", action="store_true",
        help="Machine-readable result",
    )
    _an.set_defaults(func=cmd_graph_add_task)

    _rc = _g2s.add_parser(
        "reconcile", help="Reconcile topic states from member task states"
    )
    _rc.add_argument("project", help="Project id")
    _rc.add_argument("--json", dest="json_out", action="store_true",
                     help="Machine-readable output")
    _rc.set_defaults(func=cmd_graph_reconcile)

    _mt = _g2s.add_parser(
        "mark-task", help="Topic agent: mark one task verified (or --fail)"
    )
    _mt.add_argument("task_id", help="Task (task) id to mark")
    _mt.add_argument(
        "--fail", action="store_true",
        help="Mark the task failed-verify instead of verified",
    )
    _mt.add_argument(
        "--handoff", default=None,
        help="Handoff for the task (files touched, interfaces, decisions)",
    )
    _mt.set_defaults(func=cmd_graph_mark_task)


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

    from juggle_graph_add import (
        AddTaskError, add_task, record_surfacing_conversation,
        resolve_dispatch_topic,
    )

    db = get_db(getattr(args, "db_path", None), init=True)
    if not db.get_project(args.project):
        print(f"Error: project {args.project!r} not found.", file=sys.stderr)
        sys.exit(1)

    refusal = pr_mode_refusal()
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
    # Explicit --priority wins; else a 'fix-' id defaults high so the existing
    # fix-naming convention gets fix-first dispatch with zero new flags.
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

    db = get_db(getattr(args, "db_path", None), init=True)
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


def cmd_graph_mark_task(args):
    """`juggle graph mark-task <task-id> [--fail] [--handoff '…']` — the topic
    agent's per-task completion (R9 hybrid). Maps onto the EXISTING task machine
    via mark_completion(integrate_ok=True, verify_ok=not --fail): task 'verified'
    = committed-in-topic-worktree + verify_cmd green — verified-means-MERGED
    holds at TOPIC level only (spec §2.3).

    Agent context (should_spool()): early-returns to the spool instead of the
    init=True DB open below. task_id is NOT resolved via
    resolve_thread_id_for_spool — it is a task id, not a thread."""
    from juggle_spool_cli_common import should_spool
    if should_spool():
        from dbops.spool import write_event
        from juggle_spool_paths import spool_dir
        write_event(spool_dir(), "graph_mark_task", "", "", {
            "task_id": args.task_id, "fail": getattr(args, "fail", False),
            "handoff": getattr(args, "handoff", None),
        })
        print(f"task {args.task_id} → spooled")
        return
    db = get_db(getattr(args, "db_path", None), init=True)
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
