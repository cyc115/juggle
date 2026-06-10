"""
juggle_cmd_graph — `juggle project-graph` command handlers (autopilot Phase 1).

Owns: graph-spec markdown parsing, load-time validation (cycles, unknown/dup
ids, empty prompts, verify_cmd lint, node-count sanity), and the guarded
upsert load into graph_nodes/graph_edges.
Must not own: node state semantics (dbops.db_graph) or dispatching (Phase 2).

Spec format (markdown), one `##` section per node:

    ## <node-id>: <Title>
    deps: dep1, dep2              (optional; `- deps:` also accepted)
    verify_cmd: pytest tests -q   (optional; lint-gated)
    <remaining lines = dispatch prompt>
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

from juggle_cli_common import get_db
from dbops import db_graph

MAX_NODES = 50

_HEADING_RE = re.compile(r"^##\s+([A-Za-z0-9_-]+)\s*:\s*(.+?)\s*$")
_FIELD_RE = re.compile(r"^[-*\s]*\b(deps|verify_cmd)\s*:\s*(.*)$")

# verify_cmd lint: allowlisted executables only; shells are forbidden because
# the column is LLM-populated and later executed (design DA M3).
VERIFY_CMD_ALLOWLIST = frozenset(
    {"pytest", "uv", "make", "python", "python3", "npm", "cargo", "go"}
)
_FORBIDDEN_CHARS = ("&", ";", "|", ">", "<", "`", "$(")


def parse_graph_spec(text: str) -> list[dict]:
    """Parse a graph spec markdown string into node dicts.

    Returns [{"id", "title", "deps": [..], "verify_cmd": str|None, "prompt"}].
    Duplicate ids are preserved (validation reports them).
    """
    nodes: list[dict] = []
    current: dict | None = None
    body: list[str] = []

    def _flush():
        if current is not None:
            current["prompt"] = "\n".join(body).strip()
            nodes.append(current)

    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            _flush()
            current = {"id": m.group(1), "title": m.group(2), "deps": [], "verify_cmd": None}
            body = []
            continue
        if current is None:
            continue  # preamble before first node heading
        fm = _FIELD_RE.match(line)
        if fm:
            field, value = fm.group(1), fm.group(2).strip()
            if field == "deps":
                current["deps"] = [d.strip() for d in value.split(",") if d.strip()]
            else:
                current["verify_cmd"] = value or None
            continue
        body.append(line)
    _flush()
    return nodes


def find_cycle(node_ids, edges) -> list[str] | None:
    """Kahn's algorithm over (node_id, depends_on_id) pairs. Pure.

    Returns the list of node ids stuck in a cycle, or None for a DAG.
    Lives here (load-time validation), not in dbops.db_graph — it never
    touches the DB.
    """
    indegree = {n: 0 for n in node_ids}
    dependents: dict[str, list[str]] = {n: [] for n in node_ids}
    for node, dep in edges:
        indegree[node] += 1
        dependents[dep].append(node)
    queue = [n for n, d in indegree.items() if d == 0]
    seen = 0
    while queue:
        n = queue.pop()
        seen += 1
        for m in dependents[n]:
            indegree[m] -= 1
            if indegree[m] == 0:
                queue.append(m)
    if seen == len(indegree):
        return None
    return sorted(n for n, d in indegree.items() if d > 0)


def lint_verify_cmd(cmd: str) -> str | None:
    """Return an error string if ``cmd`` fails the lint, else None."""
    for ch in _FORBIDDEN_CHARS:
        if ch in cmd:
            return f"forbidden character/operator {ch!r} in verify_cmd: {cmd!r}"
    try:
        tokens = shlex.split(cmd)
    except ValueError as e:
        return f"unparseable verify_cmd {cmd!r}: {e}"
    if not tokens:
        return "empty verify_cmd"
    exe = Path(tokens[0]).name
    if exe not in VERIFY_CMD_ALLOWLIST:
        return (
            f"executable {exe!r} not allowlisted for verify_cmd "
            f"(allowed: {sorted(VERIFY_CMD_ALLOWLIST)})"
        )
    return None


def validate_graph(nodes: list[dict]) -> list[str]:
    """Return a list of validation error strings (empty = valid)."""
    errors: list[str] = []
    if not 1 <= len(nodes) <= MAX_NODES:
        errors.append(f"node count {len(nodes)} outside sane range 1..{MAX_NODES}")
    ids = [n["id"] for n in nodes]
    seen: set[str] = set()
    for nid in ids:
        if nid in seen:
            errors.append(f"duplicate node id: {nid!r}")
        seen.add(nid)
    id_set = set(ids)
    edges: list[tuple[str, str]] = []
    for n in nodes:
        if not n["prompt"]:
            errors.append(f"empty prompt for node {n['id']!r}")
        for dep in n["deps"]:
            if dep not in id_set:
                errors.append(f"unknown dep {dep!r} on node {n['id']!r}")
            else:
                edges.append((n["id"], dep))
        if n["verify_cmd"]:
            err = lint_verify_cmd(n["verify_cmd"])
            if err:
                errors.append(f"node {n['id']!r}: {err}")
    if not errors:
        cyc = find_cycle(ids, edges)
        if cyc:
            errors.append(f"dependency cycle involving nodes: {', '.join(cyc)}")
    return errors


def _content_changed(existing: dict, spec_node: dict, spec_deps: list[str], db) -> bool:
    return (
        existing["title"] != spec_node["title"]
        or existing["prompt"] != spec_node["prompt"]
        or (existing["verify_cmd"] or None) != (spec_node["verify_cmd"] or None)
        or db_graph.get_deps(db, existing["id"]) != sorted(spec_deps)
    )


def cmd_project_graph_load(args):
    """Load (or guarded-upsert) a graph spec markdown file into graph_nodes."""
    db = get_db(getattr(args, "db_path", None), init=True)
    project = db.get_project(args.project)
    if not project:
        print(f"Error: project {args.project!r} not found.", file=sys.stderr)
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
