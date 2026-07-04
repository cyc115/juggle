"""juggle_cmd_graph_edit — `juggle graph edit-node` UPDATE command handler
(graph-node-primitives Phase7, 2026-07-03 spec §2).

Surgical field + edge edit of ONE live task. Extracted to its own module (like
juggle_cmd_graph_show) so juggle_cmd_graph_ops stays under the 300-LOC
architecture gate. Guarded, atomic, fail-loud: no write happens until every
check (state guard, unknown/self dep, empty prompt/verify-cmd, cycle) passes.

Must not own: state-machine writes (db_graph.task_transition stays sole state
writer — edit-node never changes state), parser wiring (juggle_graph_cli_parsers),
or read/rendering (juggle_cmd_graph_show, whose _node_json is reused for --json).
get_db is resolved through juggle_cmd_graph at call time so test monkeypatches
(cg.get_db) keep working.
"""
from __future__ import annotations

import sys

from dbops import db_graph


def _csv(value) -> list[str]:
    """Split a comma-separated CLI arg into a clean id list (``None`` → [])."""
    if not value:
        return []
    return [tok.strip() for tok in value.split(",") if tok.strip()]


def cmd_graph_edit_node(args) -> None:
    """`juggle graph edit-node <id> [--title][--prompt -|TXT][--priority]
    [--verify-cmd][--add-dep a,b][--rm-dep c][--json]` — edit a live task.

    Guards (fail-loud, graph untouched until all pass):
      * node missing → exit 1.
      * state not in MUTABLE_STATES (in-flight / verified / cancelled) → refuse
        exit 2 (guarded refusal, uniform "2 = guarded refusal").
      * unknown or self --add-dep target, empty prompt / verify-cmd → exit 1.
      * an --add-dep that would close a cycle → refuse exit 2 (reuses find_cycle
        over the FULL resulting edge set).
    Every accepted edit lands in ONE transaction.
    """
    import json

    import juggle_cmd_graph as cg  # lazy: shared get_db patch surface
    from juggle_graph_add import MUTABLE_STATES
    from juggle_graph_upsert import find_cycle, lint_verify_cmd

    db = cg.get_db(getattr(args, "db_path", None), init=True)
    task = db_graph.get_task(db, args.id)
    if not task:
        print(f"Error: node {args.id!r} not found.", file=sys.stderr)
        sys.exit(1)

    # Transition guard (spec §2): only MUTABLE_STATES are editable. In-flight,
    # verified, and cancelled nodes refuse with exit 2.
    if task["state"] not in MUTABLE_STATES:
        print(
            f"edit-node REFUSED — {args.id} is {task['state']!r} (not editable; "
            f"allowed: {', '.join(sorted(MUTABLE_STATES))})",
            file=sys.stderr,
        )
        sys.exit(2)

    pid = task["project_id"]
    before_deps = set(db_graph.get_deps(db, args.id))
    changes: list[str] = []

    # ── resolve field edits (validate only; no writes yet) ──
    new_title = task["title"]
    if args.title is not None:
        new_title = args.title
        changes.append("title")

    new_prompt = task["prompt"]
    if args.prompt is not None:
        prompt = args.prompt
        if prompt == "-":
            prompt = sys.stdin.read()
        new_prompt = (prompt or "").strip()
        if not new_prompt:
            print(f"Error: empty prompt for {args.id!r}.", file=sys.stderr)
            sys.exit(1)
        changes.append("prompt")

    new_verify = task.get("verify_cmd")
    if args.verify_cmd is not None:
        err = lint_verify_cmd(args.verify_cmd)
        if err:
            print(f"Error: {err}", file=sys.stderr)
            sys.exit(1)
        new_verify = args.verify_cmd
        changes.append("verify-cmd")

    new_priority = None
    if getattr(args, "priority", None) is not None:
        new_priority = args.priority
        changes.append(f"priority={args.priority}")

    # ── resolve edge edits + cycle check (validate only; no writes yet) ──
    add = _csv(getattr(args, "add_dep", None))
    rm = _csv(getattr(args, "rm_dep", None))
    edges_touched = bool(add or rm)
    new_deps = sorted(before_deps)
    if edges_touched:
        live = {n["id"] for n in db_graph.list_tasks(db, pid)}
        for dep in add:
            if dep == args.id:
                print(f"Error: task {args.id!r} cannot depend on itself.",
                      file=sys.stderr)
                sys.exit(1)
            if dep not in live:
                print(f"Error: unknown dep {dep!r} for {args.id!r}.",
                      file=sys.stderr)
                sys.exit(1)
        new_deps = sorted((before_deps | set(add)) - set(rm))
        # Full-graph cycle check over the resulting edge set (reuse find_cycle):
        # every OTHER node keeps its live deps; this node uses its new dep set.
        all_edges: list[tuple[str, str]] = []
        for n in live:
            if n == args.id:
                continue
            for d in db_graph.get_deps(db, n):
                all_edges.append((n, d))
        for d in new_deps:
            all_edges.append((args.id, d))
        cyc = find_cycle(sorted(live), all_edges)
        if cyc:
            print(
                f"edit-node REFUSED — dependency cycle would form involving: "
                f"{', '.join(cyc)}",
                file=sys.stderr,
            )
            sys.exit(2)
        for d in sorted(set(new_deps) - before_deps):
            changes.append(f"+dep {d}")
        for d in sorted(before_deps - set(new_deps)):
            changes.append(f"-dep {d}")

    if not changes:
        print(f"Error: no fields to edit for {args.id!r}.", file=sys.stderr)
        sys.exit(1)

    # ── apply atomically ──
    content_changed = (
        args.title is not None
        or args.prompt is not None
        or args.verify_cmd is not None
    )
    conn = db._connect()
    try:
        if content_changed:
            db_graph.update_task_content(
                db, args.id, title=new_title, prompt=new_prompt,
                verify_cmd=new_verify, conn=conn,
            )
        if new_priority is not None:
            db_graph.set_task_priority(db, args.id, new_priority, conn=conn)
        if edges_touched:
            db_graph.replace_edges(db, args.id, new_deps, conn=conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if getattr(args, "json_out", False):
        from juggle_cmd_graph_show import _node_json
        print(json.dumps(_node_json(db, db_graph.get_task(db, args.id))))
        return
    print(f"edited {args.id}: {', '.join(changes)}")
