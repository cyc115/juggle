"""
juggle_cmd_graph — `juggle project-graph` command handlers (autopilot Phase 1).

Owns: the `project-graph load` command handler (orchestration + guarded upsert
loop) and the PR-mode refusal policy.
Must not own: pure spec parsing/validation or single-node upsert (extracted to
juggle_graph_upsert), node state semantics (dbops.db_graph), or dispatching.

Spec format (markdown), one `##` section per node:

    ## <node-id>: <Title>
    deps: dep1, dep2              (optional; `- deps:` also accepted)
    verify_cmd: pytest tests -q   (optional; lint-gated)
    <remaining lines = dispatch prompt>
"""

from __future__ import annotations

import sys
from pathlib import Path

from juggle_cli_common import get_db
from dbops import db_graph

# Re-exported for backward compatibility (tests + callers import these from here).
from juggle_graph_upsert import (  # noqa: F401
    MAX_NODES,
    VERIFY_CMD_ALLOWLIST,
    find_cycle,
    lint_verify_cmd,
    parse_graph_spec,
    validate_graph,
)
from juggle_graph_upsert import content_changed as _content_changed  # noqa: F401


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
    success after only pushing the branch — the node went 'verified' WITHOUT
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
        "nodes 'verified' that are NOT in main and hydrate dependents with a "
        "false 'already integrated' claim. PR-mode repos are not supported "
        "by project autopilot: set push_mode to 'direct' or 'none', or drive "
        "this project without autopilot."
    )


def cmd_project_graph_load(args):
    """Load (or guarded-upsert) a graph spec markdown file into graph_nodes."""
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

    nodes = parse_graph_spec(path.read_text(encoding="utf-8"))
    errors = validate_graph(nodes)
    if errors:
        print(f"Graph spec invalid ({len(errors)} error(s)):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    # Guarded upsert: REFUSE the whole load if any protected node would change.
    existing = {n["id"]: n for n in db_graph.list_nodes(db, args.project)}
    refused = [
        n["id"]
        for n in nodes
        if n["id"] in existing
        and existing[n["id"]]["state"] in db_graph.PROTECTED_STATES
        and _content_changed(existing[n["id"]], n, n["deps"], db)
    ]
    if refused:
        print(
            "Re-load REFUSED — these nodes are dispatching/running/integrating/"
            f"verified and may not change: {', '.join(refused)}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Single transaction (DA round-2 BLOCKER-1c, 2026-06-10): per-node commits
    # left a half-applied spec when a later upsert raised. All-or-nothing.
    created = updated = unchanged = 0
    conn = db._connect()
    try:
        for n in nodes:
            prev = existing.get(n["id"])
            if prev is None:
                db_graph.create_node(
                    db,
                    node_id=n["id"],
                    project_id=args.project,
                    title=n["title"],
                    prompt=n["prompt"],
                    verify_cmd=n["verify_cmd"],
                    conn=conn,
                )
                db_graph.replace_edges(db, n["id"], sorted(n["deps"]), conn=conn)
                created += 1
            elif prev["state"] in db_graph.PROTECTED_STATES or not _content_changed(
                prev, n, n["deps"], db
            ):
                unchanged += 1
            else:
                db_graph.update_node_content(
                    db, n["id"], title=n["title"], prompt=n["prompt"],
                    verify_cmd=n["verify_cmd"], conn=conn,
                )
                db_graph.replace_edges(db, n["id"], sorted(n["deps"]), conn=conn)
                if prev["state"] != "pending":
                    db_graph.node_transition(db, n["id"], "reload", conn=conn)
                updated += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(
            f"Graph load FAILED — rolled back, no nodes changed: {e}",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        conn.close()

    # Resume the blocked tail of any node the reload just fixed (BLOCKER-1b):
    # blocked-failed ⇄ pending re-derived from current dep states.
    unblocked, _reblocked = db_graph.recompute_blocked(db, args.project)
    ready = db_graph.recompute_ready(db, args.project)
    resumed = f" resumed: {', '.join(unblocked)}." if unblocked else ""
    print(
        f"Graph loaded for project {args.project}: {len(nodes)} node(s) "
        f"({created} new, {updated} updated, {unchanged} unchanged). "
        f"ready: {', '.join(ready) if ready else '(none new)'}.{resumed}"
    )
