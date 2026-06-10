"""
juggle_graph_upsert — shared graph-spec validation + single-node upsert helpers.

Extracted from juggle_cmd_graph (2026-06-10) so both `project-graph load` (whole
spec) and `graph add-node` (one node into a live graph) call ONE validation /
upsert path. Pure validation lives here (cycle check, verify_cmd lint, per-node
field checks); the DB upsert helpers compose dbops.db_graph primitives without
duplicating its state semantics (node_transition remains the sole state writer).

Owns: load-time lint/validation (pure) + the guarded single-node upsert helper.
Must not own: node state semantics (dbops.db_graph), CLI parsing (juggle_cmd_*).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

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


def content_changed(existing: dict, spec_node: dict, spec_deps: list[str], db) -> bool:
    """True if a re-loaded spec node differs from the stored node (title /
    prompt / verify_cmd / dep set). Drives the guarded-upsert decision."""
    return (
        existing["title"] != spec_node["title"]
        or existing["prompt"] != spec_node["prompt"]
        or (existing["verify_cmd"] or None) != (spec_node["verify_cmd"] or None)
        or db_graph.get_deps(db, existing["id"]) != sorted(spec_deps)
    )


# ── single-node add into a live graph (shared by `graph add-node`) ──────────────


# Mutable states an EXISTING node touched by a new edge (a --required-by target
# gaining a dep, or a re-added --id) is allowed to be in. PROTECTED_STATES are
# refused; --deps targets (pure upstream) may be in ANY state.
MUTABLE_STATES = frozenset(
    {
        "pending",
        "ready",
        "failed-exec",
        "failed-integration",
        "failed-verify",
        "blocked-failed",
    }
)


class AddNodeError(ValueError):
    """Validation/guard failure for a single-node add. Carries a clean message;
    the CLI maps it to a nonzero exit with no partial insert."""


def validate_add_node(
    db,
    project_id: str,
    *,
    node_id: str,
    title: str,
    prompt: str,
    deps: list[str],
    required_by: list[str],
    verify_cmd: str | None,
) -> None:
    """Validate adding one node into the LIVE graph. Raises AddNodeError on the
    first material problem; returns None when the add is legal. No DB writes.

    Checks (against the union of live graph + the new node/edges):
      * empty title / empty prompt
      * verify_cmd lint (same allowlist as load)
      * every --deps id exists (upstream may be in ANY state)
      * every --required-by id exists
      * re-adding an existing --id is allowed ONLY if that node is mutable
      * any --required-by target gaining a dep must be mutable (guard)
      * full cycle check over the resulting edge set (Kahn)
    """
    if not title.strip():
        raise AddNodeError(f"empty title for node {node_id!r}")
    if not prompt.strip():
        raise AddNodeError(f"empty prompt for node {node_id!r}")
    if verify_cmd:
        err = lint_verify_cmd(verify_cmd)
        if err:
            raise AddNodeError(f"node {node_id!r}: {err}")

    live = {n["id"]: n for n in db_graph.list_nodes(db, project_id)}

    # --deps must exist (any state OK — upstream).
    for dep in deps:
        if dep == node_id:
            raise AddNodeError(f"node {node_id!r} cannot depend on itself")
        if dep not in live:
            raise AddNodeError(f"unknown dep {dep!r} for node {node_id!r}")

    # --required-by must exist and be mutable (it gains a new dependency).
    for rb in required_by:
        if rb == node_id:
            raise AddNodeError(f"node {node_id!r} cannot be required by itself")
        if rb not in live:
            raise AddNodeError(f"unknown required-by target {rb!r} for node {node_id!r}")
        if live[rb]["state"] not in MUTABLE_STATES:
            raise AddNodeError(
                f"refusing to add a dependency to {rb!r}: it is "
                f"{live[rb]['state']!r} (protected) — required-by targets must be "
                f"pending/ready/failed-*/blocked-failed"
            )

    # Re-adding an existing id: allowed only if that node is mutable.
    if node_id in live and live[node_id]["state"] not in MUTABLE_STATES:
        raise AddNodeError(
            f"node id {node_id!r} already exists in state "
            f"{live[node_id]['state']!r} (protected) — cannot re-add"
        )

    # Full cycle check over the resulting edge set (existing + new).
    all_ids = set(live) | {node_id}
    edges: list[tuple[str, str]] = []
    for n in live:
        if n == node_id:
            continue  # the new node's own edges are rebuilt below
        for d in db_graph.get_deps(db, n):
            edges.append((n, d))
    for d in deps:
        edges.append((node_id, d))
    for rb in required_by:
        edges.append((rb, node_id))
    cyc = find_cycle(sorted(all_ids), edges)
    if cyc:
        raise AddNodeError(f"dependency cycle would form involving: {', '.join(cyc)}")


def add_node(
    db,
    project_id: str,
    *,
    node_id: str,
    title: str,
    prompt: str,
    deps: list[str],
    required_by: list[str],
    verify_cmd: str | None,
) -> dict:
    """Validated, atomic, guarded insert of ONE node into a live graph.

    Validates first (validate_add_node — raises AddNodeError on any problem,
    nothing written). Then, in a single transaction: upsert the node as
    'pending' (re-add resets a mutable existing node via reload), set its --deps
    edges, and add a depends_on edge from each --required-by target to the new
    node. Commits, then runs the existing readiness recompute so the new node
    becomes 'ready' iff all its deps are verified, and any downstream node that
    now waits on the unfinished new node is demoted (recompute_blocked +
    recompute_ready — the sanctioned seams; node_transition stays sole writer).

    Returns {"node_id", "state", "downstream_changed": [{"id","from","to"}]}.
    """
    validate_add_node(
        db, project_id, node_id=node_id, title=title, prompt=prompt,
        deps=deps, required_by=required_by, verify_cmd=verify_cmd,
    )

    live = {n["id"]: n for n in db_graph.list_nodes(db, project_id)}
    before = {n["id"]: n["state"] for n in live.values()}

    conn = db._connect()
    try:
        if node_id in live:
            # Re-add of a mutable existing node: reset content + state to pending.
            db_graph.update_node_content(
                db, node_id, title=title, prompt=prompt, verify_cmd=verify_cmd,
                conn=conn,
            )
            if live[node_id]["state"] != "pending":
                db_graph.node_transition(db, node_id, "reload", conn=conn)
        else:
            db_graph.create_node(
                db, node_id=node_id, project_id=project_id, title=title,
                prompt=prompt, verify_cmd=verify_cmd, conn=conn,
            )
        db_graph.replace_edges(db, node_id, sorted(deps), conn=conn)

        # Downstream inserts: each --required-by target gains a dep on node_id.
        for rb in required_by:
            new_deps = sorted(set(db_graph.get_deps(db, rb)) | {node_id})
            db_graph.replace_edges(db, rb, new_deps, conn=conn)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    finally:
        if not conn.in_transaction:
            try:
                conn.close()
            except Exception:
                pass

    # Recompute readiness/blocking through the existing seams. A required-by
    # target that was ready/pending may now need to wait on the unfinished new
    # node: recompute_blocked re-derives blocked-failed; recompute_ready will
    # NOT keep a node ready whose new dep is unverified (ready_eligible only
    # promotes pending nodes with all deps verified) — but a node already in
    # 'ready' is not auto-demoted by recompute_ready, so demote it here.
    _demote_unsatisfied_ready(db, project_id)
    db_graph.recompute_blocked(db, project_id)
    db_graph.recompute_ready(db, project_id)

    after = {n["id"]: n["state"] for n in db_graph.list_nodes(db, project_id)}
    downstream_changed = [
        {"id": nid, "from": before[nid], "to": after[nid]}
        for nid in before
        if nid != node_id and before[nid] != after[nid]
    ]
    return {
        "node_id": node_id,
        "state": after.get(node_id, "pending"),
        "downstream_changed": downstream_changed,
    }


def _demote_unsatisfied_ready(db, project_id: str) -> None:
    """Demote any 'ready' node whose deps are no longer all verified back to
    'pending'. A --required-by insert can add an unverified dep to a node that
    was already 'ready'; recompute_ready only promotes pending→ready, so the
    demotion is applied here through the sole state writer (the 'unready'
    event). A 'ready' node has no thread bound, so the transition is safe."""
    for node in db_graph.list_nodes(db, project_id):
        if node["state"] == "ready" and db_graph.unverified_deps(db, node["id"]):
            db_graph.node_transition(db, node["id"], "unready")
