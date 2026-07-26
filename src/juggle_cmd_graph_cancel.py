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


def _get_node_and_kind(db, node_id: str):
    """Resolve ``node_id`` as either a task or a topic node (2026-07-25 CLI GAP:
    cancel-node previously only ever looked up kind='task', so a stuck
    failed-integration TOPIC node had no CLI path to clear it — only a raw DB
    write worked). Returns ``(node_dict, kind)`` where kind is 'task' or
    'topic', or ``(None, None)`` if neither resolves."""
    from dbops import db_graph, db_topics

    task = db_graph.get_task(db, node_id)
    if task is not None:
        return task, "task"
    topic = db_topics.get_topic(db, node_id)
    if topic is not None:
        return topic, "topic"
    return None, None


def _transition(db, node_id: str, kind: str, event: str) -> str:
    """Dispatch the 'cancel' event through the writer sanctioned for ``kind``
    — db_graph.task_transition for a task, db_topics.topic_transition for a
    topic. Both delegate the same (state, event) decision to the unified
    node_transition machine; only the read/write scope differs."""
    from dbops import db_graph, db_topics

    if kind == "topic":
        return db_topics.topic_transition(db, node_id, event)
    return db_graph.task_transition(db, node_id, event)


def cmd_graph_cancel_node(args):
    """`juggle graph cancel-node <id> [--cascade] [--reason TXT] [--dry-run] [--json]`
    — set a task OR topic node to the soft-terminal 'cancelled' state.

    Accepts topic-level nodes as well as tasks (2026-07-25 CLI GAP: a topic
    stuck at 'failed-integration' had no CLI path to clear it — mark-task and
    cancel-node both rejected any non-task node as "not found"; only a raw DB
    write worked). cancel-node is the right lever for a stuck TOPIC: its
    soft-terminal 'cancelled' state is kind-agnostic in the state machine
    (db_graph.task_transition and db_topics.topic_transition both delegate to
    the same node_transition(..., "task") decision) — mark-task, by contrast,
    is per-task COMPLETION semantics (verify_ok tri-state, handoff) that don't
    apply to a topic, so it stays task-only.

    State is written ONLY through the writer sanctioned for the node's kind —
    never a raw UPDATE. Guarantees:
      * idempotent — an already-'cancelled' node is a no-op (exit 0);
      * refuses (exit 2) any node in an in-flight/protected state (PROTECTED_STATES);
      * for a TASK, WITHOUT --cascade refuses (exit 2) if it has any
        NON-terminal dependent — forcing an explicit --cascade rather than
        silently orphaning; WITH --cascade, cancels the whole
        transitive-dependent subtree (visit-once/diamond-safe) with an
        all-or-nothing in-flight guard;
      * a TOPIC never cascades (exit 2 on --cascade) — topic-level deps are
        DERIVED from underlying task edges, not stored as direct node_edges on
        the topic itself, so the task-dependents closure doesn't apply;
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

    node, kind = _get_node_and_kind(db, args.id)
    if node is None:
        _cancel_fail(json_out, f"node {args.id!r} not found.", code=1)

    # Idempotent: already cancelled → no-op (exit 0).
    if node["state"] == "cancelled":
        if json_out:
            print(json.dumps({"ok": True, "id": args.id,
                              "cancelled": [], "already": True}))
        else:
            print(f"{args.id} already cancelled (no-op)")
        return

    if cascade and kind == "topic":
        _cancel_fail(
            json_out,
            f"{args.id} is a topic — --cascade is only supported for task "
            "nodes (topic-level deps are derived, not directly cascadable)",
            code=2,
        )

    # Build the cancel set (root + optionally its transitive dependents).
    if cascade:
        cancel_set = [args.id] + _dependents_closure(db, args.id)
    elif kind == "task":
        non_terminal = [
            d for d in db_graph.get_dependents(db, args.id)
            if (dep := db_graph.get_task(db, d)) and dep["state"] not in _TASK_TERMINAL
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
    else:  # topic, no cascade
        cancel_set = [args.id]

    # All-or-nothing in-flight guard across the WHOLE set (spec addendum): never
    # cancel a subtree if any node in it is being actively worked / verified.
    # Kind-aware lookup (2026-07-26 fix): the old task-only get_task silently
    # let a topic root bypass this guard entirely.
    blockers = [
        n for n in cancel_set
        if (found := _get_node_and_kind(db, n)[0]) and found["state"] in db_graph.PROTECTED_STATES
    ]
    if blockers:
        detail = ", ".join(
            f"{n} ({_get_node_and_kind(db, n)[0]['state']})" for n in blockers
        )
        _cancel_fail(
            json_out, f"refusing cancel — in-flight/protected node(s): {detail}",
            code=2,
        )

    # Nodes actually transitioned (a cascade closure may already hold cancelled ones).
    to_cancel = [
        n for n in cancel_set if _get_node_and_kind(db, n)[0]["state"] != "cancelled"
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
        _, n_kind = _get_node_and_kind(db, n)
        _transition(db, n, n_kind, "cancel")
        if reason:
            db_graph.set_cancel_reason(db, n, reason)
        affected.append(n)
        cancelled_node, _ = _get_node_and_kind(db, n)
        if cancelled_node and cancelled_node.get("topic_id"):
            topics.add(cancelled_node["topic_id"])

    # Re-derive owning-topic states so 'cancelled' drops from done/total (DA-B1).
    # Best-effort (2026-07-26 fix, REGRESSION PIN
    # test_cancel_task_with_orphaned_topic_ref_does_not_crash): a task's
    # parent_id may reference a topic id that doesn't resolve to an actual
    # topic node (orphaned ref) — reconcile runs AFTER the mutation above
    # already committed, so it must never crash the command over a stale
    # reference; skip a topic id that doesn't exist rather than raising.
    for topic_id in topics:
        if db_topics.get_topic(db, topic_id) is not None:
            db_topics.reconcile_topic_state(db, topic_id)

    if json_out:
        print(json.dumps({"ok": True, "id": args.id, "cancelled": affected}))
        return
    extra = len(affected) - 1
    tail = (f" (+{extra} dependent{'s' if extra != 1 else ''})"
            if extra > 0 else "")
    print(f"cancelled {args.id}{tail}")
