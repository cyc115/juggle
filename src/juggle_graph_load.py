"""
juggle_graph_load — `project-graph load` orchestration (autopilot plan store).

Extracted from juggle_cmd_graph (2026-06-11, R9 topic tier) so the whole-spec
load path (parse → validate → guarded one-transaction topic+task upsert) lives
in one focused module and juggle_cmd_graph stays under the architecture line
budget. Owns the load handler only.

Must not own: pure spec parsing/validation (juggle_graph_upsert), task/topic
state semantics (dbops.db_graph / dbops.db_topics), or the PR-mode refusal
policy / live add-task (juggle_cmd_graph — pr_mode_refusal imported lazily to
keep `_git_root` monkeypatchable on that module and avoid an import cycle).

Spec format (markdown), 3-tier with legacy flat fallback:

    ## topic <topic-id>: <Topic title>
    <objective lines>
    ### <task-id>: <Task title>
    deps: dep1, dep2              (optional; cross-topic deps allowed)
    verify_cmd: pytest tests -q   (optional; lint-gated)
    <remaining lines = dispatch prompt>

A spec with no `## topic` headings loads as before — each flat `## task`
becomes a synthetic single-task topic `T-<task-id>`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from juggle_cli_common import get_db
from dbops import db_graph, db_topics
from juggle_graph_upsert import (
    content_changed as _content_changed,
    parse_topics_spec,
    validate_topics,
)


def cmd_project_graph_load(args):
    """Load (or guarded-upsert) a graph spec markdown file into graph_topics +
    graph_tasks."""
    from juggle_cmd_graph import pr_mode_refusal  # lazy: avoid import cycle

    db = get_db(getattr(args, "db_path", None), init=True)
    project = db.get_project(args.project)
    if not project:
        print(f"Error: project {args.project!r} not found.", file=sys.stderr)
        sys.exit(1)

    refusal = pr_mode_refusal()
    if refusal:
        print(f"Error: {refusal}", file=sys.stderr)
        sys.exit(1)

    path = Path(args.file)
    if not path.exists():
        print(f"Error: spec file not found: {path}", file=sys.stderr)
        sys.exit(1)

    topics = parse_topics_spec(path.read_text(encoding="utf-8"))
    errors = validate_topics(topics)
    if errors:
        print(f"Graph spec invalid ({len(errors)} error(s)):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    # Flatten the task tier; remember each task's owning topic for topic_id.
    tasks = [n for t in topics for n in t["tasks"]]
    task_topic = {n["id"]: t["id"] for t in topics for n in t["tasks"]}

    # Guarded upsert: REFUSE the whole load if any protected task would change.
    existing = {n["id"]: n for n in db_graph.list_tasks(db, args.project)}
    refused = [
        n["id"]
        for n in tasks
        if n["id"] in existing
        and existing[n["id"]]["state"] in db_graph.PROTECTED_STATES
        and _content_changed(existing[n["id"]], n, n["deps"], db)
    ]
    if refused:
        print(
            "Re-load REFUSED — these tasks are dispatching/running/integrating/"
            f"verified and may not change: {', '.join(refused)}",
            file=sys.stderr,
        )
        sys.exit(1)

    existing_topics = {t["id"]: t for t in db_topics.list_topics(db, args.project)}

    # Single transaction (DA round-2 BLOCKER-1c, 2026-06-10): per-task commits
    # left a half-applied spec when a later upsert raised. All-or-nothing.
    created = updated = unchanged = 0
    conn = db._connect()
    try:
        # Topics first (graph_tasks.topic_id FK references graph_topics). A topic
        # in a PROTECTED_STATE keeps its state untouched; title/objective of a
        # non-protected existing topic may update.
        for t in topics:
            et = existing_topics.get(t["id"])
            if et is None:
                db_topics.create_topic(
                    db, topic_id=t["id"], project_id=args.project,
                    title=t["title"], objective=t.get("objective", ""), conn=conn,
                )
            elif et["state"] not in db_graph.PROTECTED_STATES:
                conn.execute(
                    "UPDATE graph_topics SET title=?, objective=? WHERE id=?",
                    (t["title"], t.get("objective", ""), t["id"]),
                )
        for n in tasks:
            prev = existing.get(n["id"])
            if prev is None:
                db_graph.create_task(
                    db,
                    task_id=n["id"],
                    project_id=args.project,
                    title=n["title"],
                    prompt=n["prompt"],
                    verify_cmd=n["verify_cmd"],
                    conn=conn,
                )
                db_graph.set_task_topic(db, n["id"], task_topic[n["id"]], conn=conn)
                db_graph.replace_edges(db, n["id"], sorted(n["deps"]), conn=conn)
                created += 1
            elif prev["state"] in db_graph.PROTECTED_STATES or not _content_changed(
                prev, n, n["deps"], db
            ):
                unchanged += 1
            else:
                db_graph.update_task_content(
                    db, n["id"], title=n["title"], prompt=n["prompt"],
                    verify_cmd=n["verify_cmd"], conn=conn,
                )
                db_graph.set_task_topic(db, n["id"], task_topic[n["id"]], conn=conn)
                db_graph.replace_edges(db, n["id"], sorted(n["deps"]), conn=conn)
                if prev["state"] != "open":
                    db_graph.task_transition(db, n["id"], "reload", conn=conn)
                updated += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(
            f"Graph load FAILED — rolled back, no tasks changed: {e}",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        conn.close()

    # Self-heal graph drift (DEFECT #4907): the guarded upsert skips
    # set_task_topic for protected/unchanged tasks, so a verified task whose node
    # carries a stale/NULL parent_id is never re-linked by the reload itself.
    # Re-link parent_id + resync state from the legacy authoritative graph_tasks
    # for the whole project so orphan detection never sees a childless topic.
    from dbops.db_graph_reconcile import reconcile_node_parentage
    reconcile_node_parentage(db, project_id=args.project)

    # Resume the blocked tail of any task the reload just fixed (BLOCKER-1b):
    # blocked-failed ⇄ pending re-derived from current dep states.
    unblocked, _reblocked = db_graph.recompute_blocked(db, args.project)
    ready = db_graph.recompute_ready(db, args.project)
    resumed = f" resumed: {', '.join(unblocked)}." if unblocked else ""
    print(
        f"Graph loaded for project {args.project}: {len(tasks)} task(s) "
        f"({created} new, {updated} updated, {unchanged} unchanged). "
        f"ready: {', '.join(ready) if ready else '(none new)'}.{resumed}"
    )
