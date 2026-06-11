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

from juggle_cli_common import get_db
# db_graph re-exported (used by add-node + the atomicity regression pin, which
# monkeypatches cg.db_graph — the same module object juggle_graph_load uses).
from dbops import db_graph, db_topics  # noqa: F401

# Re-exported for backward compatibility (tests + callers import these from here).
from juggle_graph_upsert import (  # noqa: F401
    MAX_NODES,
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


def _is_synthetic_topic(topic_id: str) -> bool:
    """Synthetic single-task topics (migration-37 / flat-spec fallback) are
    named 'T-<node-id>' or 'T#<node-id>'. A project with ONLY synthetic topics
    is treated as a flat graph for add-node (topic optional)."""
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


def register_graph_parsers(subparsers) -> None:
    """Register the `project-graph` (plan store) and `graph` (live edits) groups.

    Kept here (next to the handlers) so juggle_cli_parsers_misc stays under the
    architecture line budget and the graph CLI surface lives in one place.
    """
    p_graph = subparsers.add_parser(
        "project-graph", help="Project task-graph (autopilot plan store)"
    )
    _gs = p_graph.add_subparsers(dest="graph_command", required=True)
    _g = _gs.add_parser("load", help="Load/upsert a graph spec markdown file")
    _g.add_argument("file", help="Path to graph spec markdown")
    _g.add_argument("--project", required=True, help="Project id the graph belongs to")
    _g.set_defaults(func=cmd_project_graph_load)

    p_g2 = subparsers.add_parser("graph", help="Live project task-graph edits")
    _g2s = p_g2.add_subparsers(dest="graph2_command", required=True)
    _an = _g2s.add_parser(
        "add-node", help="Inject one new node into an existing project graph"
    )
    _an.add_argument("--project", required=True, help="Project id the graph belongs to")
    _an.add_argument("--id", required=True, help="Stable node id")
    _an.add_argument("--title", required=True, help="Node title")
    _an.add_argument(
        "--prompt", default=None,
        help="Dispatch prompt (omit or pass '-' to read from stdin)",
    )
    _an.add_argument(
        "--deps", default=None,
        help="Comma-separated EXISTING node ids this node depends on (upstream)",
    )
    _an.add_argument(
        "--required-by", dest="required_by", default=None,
        help="Comma-separated EXISTING node ids that gain a dep on this node",
    )
    _an.add_argument(
        "--topic", default=None,
        help="Owning topic id (REQUIRED when the project has real topics; "
        "omit on a flat project to auto-create a synthetic 'T-<node-id>' topic)",
    )
    _an.add_argument(
        "--verify-cmd", dest="verify_cmd", default=None,
        help="Verification command (lint-gated, same allowlist as load)",
    )
    _an.add_argument(
        "--json", dest="json_out", action="store_true",
        help="Machine-readable result",
    )
    _an.set_defaults(func=cmd_graph_add_node)


def _csv(value) -> list[str]:
    """Split a comma-separated CLI arg into a clean id list (``None`` → [])."""
    if not value:
        return []
    return [tok.strip() for tok in value.split(",") if tok.strip()]


def cmd_graph_add_node(args):
    """Inject one new node into an EXISTING project graph mid-execution.

    Validated, atomic, guarded upsert via juggle_graph_upsert.add_node — refuses
    (nonzero exit, graph unchanged) on unknown deps, a cycle, an empty prompt, a
    verify_cmd lint failure, or touching a protected node. Supports args-or-stdin
    for --prompt so a long dispatch prompt can be piped.
    """
    import json

    from juggle_graph_add import AddNodeError, add_node

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

    # Resolve the owning topic BEFORE any graph mutation (so a missing --topic
    # exits without touching the graph). On a project with real (non-synthetic)
    # topics --topic is REQUIRED; on a flat project it auto-creates 'T-<node-id>'.
    topic = getattr(args, "topic", None)
    project_topics = db_topics.list_topics(db, args.project)
    has_real_topic = any(not _is_synthetic_topic(t["id"]) for t in project_topics)
    auto_topic = False
    if topic:
        if db_topics.get_topic(db, topic) is None:
            print(f"add-node REFUSED — unknown topic {topic!r}.", file=sys.stderr)
            sys.exit(1)
    elif has_real_topic:
        print(
            "add-node REFUSED — this project has topics; --topic <id> is "
            "required.",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        topic = f"T-{args.id}"
        auto_topic = True

    try:
        result = add_node(
            db, args.project,
            node_id=args.id, title=args.title, prompt=prompt,
            deps=_csv(args.deps), required_by=_csv(args.required_by),
            verify_cmd=args.verify_cmd,
        )
    except AddNodeError as e:
        if getattr(args, "json_out", False):
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"add-node REFUSED — graph unchanged: {e}", file=sys.stderr)
        sys.exit(1)

    # Assign the topic: auto-create the synthetic topic if needed, then point
    # the new node at it (topic_id FK).
    if auto_topic and db_topics.get_topic(db, topic) is None:
        db_topics.create_topic(
            db, topic_id=topic, project_id=args.project, title=args.title,
        )
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_nodes SET topic_id=? WHERE id=?", (topic, args.id)
        )
        conn.commit()

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
        f"Added node {result['node_id']!r} to project {args.project} "
        f"(state: {result['state']}).{tail}"
    )
