"""juggle_cmd_runs — CLI for the durable agent I/O ledger (agent_runs).

Owns: `juggle runs [filters]` (list), `juggle runs show <id>` (full detail),
and `juggle runs prune --older-than <Nd>` (manual retention). Read-mostly
surface over RunsMixin; the ledger is written at the dispatch/completion choke
points, never here.
"""

from __future__ import annotations

import json

from juggle_cli_common import get_db


def _teaser(text, n=60):
    if not text:
        return ""
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _parse_days(raw) -> int:
    """Accept '90d' or '90' → 90 (int days). Raises ValueError otherwise."""
    s = str(raw).strip().lower().rstrip("d")
    return int(s)


def cmd_runs_list(args):
    db = get_db(getattr(args, "db_path", None), init=True)
    runs = db.get_runs(
        project_id=getattr(args, "project", None),
        topic_id=getattr(args, "topic", None),
        node_id=getattr(args, "node", None),
        thread_id=getattr(args, "thread", None),
        limit=getattr(args, "limit", None),
    )
    if getattr(args, "json_out", False):
        print(json.dumps(runs, indent=2))
        return
    if not runs:
        print("No runs.")
        return
    print(
        f"{'ID':>5}  {'STATUS':<10} {'ROLE':<9} {'MODEL':<10} "
        f"{'PROJECT':<10} {'TOPIC':<8} {'NODE':<8} {'DISPATCHED':<26} INPUT→OUTPUT"
    )
    for r in runs:
        io = f"{_teaser(r['input_prompt'], 32)} → {_teaser(r['output'], 24)}"
        print(
            f"{r['id']:>5}  {r['status']:<10} {(r['role'] or ''):<9} "
            f"{(r['model'] or ''):<10} {(r['project_id'] or ''):<10} "
            f"{(r['topic_id'] or ''):<8} {(r['node_id'] or ''):<8} "
            f"{(r['dispatched_at'] or ''):<26} {io}"
        )


def cmd_runs_show(args):
    db = get_db(getattr(args, "db_path", None), init=True)
    run = db.get_run(int(args.run_id))
    if not run:
        print(f"Error: run {args.run_id} not found.")
        raise SystemExit(1)
    if getattr(args, "json_out", False):
        print(json.dumps(run, indent=2))
        return
    print(f"Run {run['id']} — {run['status']}")
    for k in ("thread_id", "project_id", "topic_id", "node_id", "agent_id",
              "role", "model", "harness", "dispatched_at", "completed_at"):
        print(f"  {k:<14} {run.get(k)}")
    print("\n--- INPUT (full sent prompt) ---")
    print(run["input_prompt"])
    print("\n--- OUTPUT ---")
    print(run.get("output") or "(none)")
    print("\n--- DIFFSTAT ---")
    print(run.get("diffstat") or "(none)")


def cmd_runs_prune(args):
    db = get_db(getattr(args, "db_path", None), init=True)
    try:
        days = _parse_days(args.older_than)
    except ValueError:
        print(f"Error: invalid --older-than {args.older_than!r} (use e.g. 90d or 90).")
        raise SystemExit(1)
    n = db.prune_runs(older_than_days=days)
    print(f"Pruned {n} run(s) older than {days}d.")


def register_runs_parsers(subparsers) -> None:
    """Register `juggle runs` and its subcommands."""
    p = subparsers.add_parser("runs", help="Agent I/O ledger (input/output per dispatch)")
    sub = p.add_subparsers(dest="runs_command")

    p.add_argument("--project", default=None, help="Filter by project_id")
    p.add_argument("--topic", default=None, help="Filter by topic_id")
    p.add_argument("--node", default=None, help="Filter by node_id")
    p.add_argument("--thread", default=None, help="Filter by thread_id")
    p.add_argument("--limit", type=int, default=None, help="Max rows")
    p.add_argument("--json", dest="json_out", action="store_true", help="JSON output")
    p.set_defaults(func=cmd_runs_list)

    p_show = sub.add_parser("show", help="Full input/output/diffstat for one run")
    p_show.add_argument("run_id")
    p_show.add_argument("--json", dest="json_out", action="store_true")
    p_show.set_defaults(func=cmd_runs_show)

    p_prune = sub.add_parser("prune", help="Delete runs older than a cutoff")
    p_prune.add_argument("--older-than", dest="older_than", required=True,
                         metavar="Nd", help="Cutoff, e.g. 90d or 90")
    p_prune.set_defaults(func=cmd_runs_prune)
