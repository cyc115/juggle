"""juggle_graph_cli_parsers — argparse registration for the `graph` command
group (load / add-task / reconcile / mark-task).

Extracted from juggle_cmd_graph (2026-07-03, architecture-gate LOC budget:
juggle_cmd_graph was at the 300-line ceiling and needed headroom for
T-fix-dispatch-plan-spec-provision's --plan/--spec flags).

Owns: argparse wiring only. Must not own: command handler logic (juggle_cmd_graph).
"""
from __future__ import annotations


def register_graph_parsers(subparsers) -> None:
    """Register the `graph` group: load (plan store) + live edits (add-task/
    reconcile/mark-task).

    P9 G2 folded the former `project-graph load` into `graph load`; legacy
    `project-graph …` resolves via the alias shim. Kept next to the handler
    imports so the graph CLI surface lives in one place.
    """
    from juggle_cmd_graph import (
        cmd_graph_add_task, cmd_graph_cancel_node, cmd_graph_learn,
        cmd_graph_mark_task, cmd_graph_reconcile, cmd_graph_show,
        cmd_project_graph_load,
    )

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
        "--plan", default=None,
        help="Plan file path for the owning topic (first-set-wins — never "
        "overrides a plan/spec already recorded by `graph load`)",
    )
    _an.add_argument(
        "--spec", default=None,
        help="Spec file path for the owning topic (first-set-wins — never "
        "overrides a plan/spec already recorded by `graph load`)",
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

    _ln = _g2s.add_parser(
        "learn",
        help="Record a node's cross-session learnings (≤300 chars, verbatim, "
        "overwrite-on-rewrite)",
    )
    _ln.add_argument("node_id", help="Node (task) id to attach the learning to")
    _ln.add_argument(
        "text",
        help="Learning text (≤300 chars): non-obvious gotchas / constraints / "
        "decision-why for the NEXT agent — NOT status (the handoff owns that)",
    )
    _ln.set_defaults(func=cmd_graph_learn)

    _sh = _g2s.add_parser(
        "show", help="Read the task graph (project rollup / node detail); pure read"
    )
    _sh.add_argument("--project", default=None, help="Show one project's nodes")
    _sh.add_argument("--node", default=None, help="Show a single node's detail")
    _sh.add_argument("--state", default=None,
                     help="Filter nodes to this state (e.g. failed-verify)")
    _sh.add_argument("--json", dest="json_out", action="store_true",
                     help="Machine-readable output (compact)")
    _sh.set_defaults(func=cmd_graph_show)

    _cn = _g2s.add_parser(
        "cancel-node",
        help="Soft-cancel a node → 'cancelled' (+ --cascade for the subtree)",
    )
    _cn.add_argument("id", help="Task/node id to cancel")
    _cn.add_argument("--cascade", action="store_true",
                     help="Also cancel every transitive dependent (subtree prune)")
    _cn.add_argument("--reason", default=None,
                     help="Reason string stored on the cancelled node(s)")
    _cn.add_argument("--dry-run", dest="dry_run", action="store_true",
                     help="Report the affected id set without mutating")
    _cn.add_argument("--json", dest="json_out", action="store_true",
                     help="Machine-readable output (compact)")
    _cn.set_defaults(func=cmd_graph_cancel_node)
