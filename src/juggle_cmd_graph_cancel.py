"""juggle_cmd_graph_cancel — `juggle graph cancel-node` mutator (graph-node-
primitives Phase5, 2026-07-03 spec §3 + DA-B1/B4 + addendum).

Extracted to its own module (mirroring juggle_cmd_graph_show) so the shared
mutator module juggle_cmd_graph_ops stays under the 300-LOC architecture gate.

Sets a node to the soft-terminal ``cancelled`` state — written ONLY through
``db_graph.task_transition`` (the ``cancel`` event), never a raw UPDATE. Owns:
the cancel handler, its dependents-closure BFS, and the refusal/exit helper.
Must not own: the state machine (dbops.db_node_machine), task CRUD/edges
(dbops.db_graph), parser wiring (juggle_graph_cli_parsers), or the other mutators
(juggle_cmd_graph_ops). get_db is resolved through juggle_cmd_graph at call time
so test monkeypatches (cg.get_db) keep working.
"""

from __future__ import annotations

import sys


def _dependents_closure(db, root: str) -> list[str]:
    """Transitive dependents of ``root`` (visit-once, diamond-safe), root excluded.

    Same BFS shape as ``db_graph.propagate_failure`` (db_graph_marking.py) — a
    diamond dependent reachable by two paths is visited exactly once — but returns
    the closure so the caller can apply the ``cancel`` event to the whole subtree.
    """
    from dbops import db_graph

    seen = {root}
    order: list[str] = []
    queue = [root]
    while queue:
        for dep_id in db_graph.get_dependents(db, queue.pop(0)):
            if dep_id in seen:
                continue
            seen.add(dep_id)
            order.append(dep_id)
            queue.append(dep_id)
    return order


def _cancel_fail(json_out: bool, msg: str, code: int):
    """Uniform cancel-node refusal/error exit — exit 2 = guarded refusal, 1 =
    error (spec §22 exit-code contract). ``--json`` emits ``{ok:false,error}``."""
    import json

    if json_out:
        print(json.dumps({"ok": False, "error": msg}))
    else:
        print(f"Error: {msg}", file=sys.stderr)
    sys.exit(code)


def cmd_graph_cancel_node(args):
    """`juggle graph cancel-node <id> [--cascade] [--reason TXT] [--dry-run] [--json]`
    — set a node to the soft-terminal 'cancelled' state.

    State is written ONLY through ``db_graph.task_transition`` (the 'cancel'
    event → 'cancelled'); never a raw UPDATE. Guarantees:
      * idempotent — an already-'cancelled' node is a no-op (exit 0);
      * refuses (exit 2) any node in an in-flight/protected state (PROTECTED_STATES);
      * WITHOUT --cascade, refuses (exit 2) if the node has any NON-terminal
        dependent — forcing an explicit --cascade rather than silently orphaning;
      * WITH --cascade, cancels the whole transitive-dependent subtree
        (visit-once/diamond-safe) with an all-or-nothing in-flight guard;
      * --dry-run reports the affected id set and mutates nothing (prod-DB
        fail-closed posture).
    """
    import json

    import juggle_cmd_graph as cg  # lazy: break the re-export import cycle
    from dbops import db_graph, db_topics
    from juggle_cmd_agents_graph_topics import _TASK_TERMINAL

    db = cg.get_db(getattr(args, "db_path", None), init=True)
    cascade = getattr(args, "cascade", False)
    reason = getattr(args, "reason", None)
    dry_run = getattr(args, "dry_run", False)
    json_out = getattr(args, "json_out", False)

    task = db_graph.get_task(db, args.id)
    if task is None:
        _cancel_fail(json_out, f"node {args.id!r} not found.", code=1)

    # Idempotent: already cancelled → no-op (exit 0).
    if task["state"] == "cancelled":
        if json_out:
            print(json.dumps({"ok": True, "id": args.id,
                              "cancelled": [], "already": True}))
        else:
            print(f"{args.id} already cancelled (no-op)")
        return

    # Build the cancel set (root + optionally its transitive dependents).
    if cascade:
        cancel_set = [args.id] + _dependents_closure(db, args.id)
    else:
        non_terminal = [
            d for d in db_graph.get_dependents(db, args.id)
            if (t := db_graph.get_task(db, d)) and t["state"] not in _TASK_TERMINAL
        ]
        if non_terminal:
            _cancel_fail(
                json_out,
                f"{args.id} has non-terminal dependent(s): "
                f"{', '.join(non_terminal)} — re-run with --cascade to cancel "
                "the subtree (or reparent them first)",
                code=2,
            )
        cancel_set = [args.id]

    # All-or-nothing in-flight guard across the WHOLE set (spec addendum): never
    # cancel a subtree if any node in it is being actively worked / verified.
    blockers = [
        n for n in cancel_set
        if (t := db_graph.get_task(db, n)) and t["state"] in db_graph.PROTECTED_STATES
    ]
    if blockers:
        detail = ", ".join(
            f"{n} ({db_graph.get_task(db, n)['state']})" for n in blockers
        )
        _cancel_fail(
            json_out, f"refusing cancel — in-flight/protected node(s): {detail}",
            code=2,
        )

    # Nodes actually transitioned (a cascade closure may already hold cancelled ones).
    to_cancel = [
        n for n in cancel_set if db_graph.get_task(db, n)["state"] != "cancelled"
    ]

    if dry_run:
        if json_out:
            print(json.dumps({"ok": True, "id": args.id,
                              "dry_run": True, "affected": to_cancel}))
        else:
            print(f"dry-run: would cancel {len(to_cancel)} node(s): "
                  f"{', '.join(to_cancel)}")
        return

    affected: list[str] = []
    topics: set[str] = set()
    for n in to_cancel:
        db_graph.task_transition(db, n, "cancel")
        if reason:
            db_graph.set_cancel_reason(db, n, reason)
        affected.append(n)
        node = db_graph.get_task(db, n)
        if node and node.get("topic_id"):
            topics.add(node["topic_id"])

    # Re-derive owning-topic states so 'cancelled' drops from done/total (DA-B1).
    for topic_id in topics:
        db_topics.reconcile_topic_state(db, topic_id)

    if json_out:
        print(json.dumps({"ok": True, "id": args.id, "cancelled": affected}))
        return
    extra = len(affected) - 1
    tail = (f" (+{extra} dependent{'s' if extra != 1 else ''})"
            if extra > 0 else "")
    print(f"cancelled {args.id}{tail}")
