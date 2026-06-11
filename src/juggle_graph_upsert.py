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
_TOPIC_HEADING_RE = re.compile(r"^##\s+topic\s+([A-Za-z0-9_-]+)\s*:\s*(.+?)\s*$")
_TASK_HEADING_RE = re.compile(r"^###\s+([A-Za-z0-9_-]+)\s*:\s*(.+?)\s*$")

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


def parse_topics_spec(text: str) -> list[dict]:
    """Parse a 3-tier spec: [{'id','title','objective','tasks':[node dicts]}].

    LEGACY FALLBACK (R6): a spec with no `## topic` headings parses via
    parse_graph_spec and wraps each flat node in a synthetic 1-task topic
    'T-<id>' — the exact shape migration 37 produces. A spec mixing both
    heading forms gets a '_mixed' marker for validate_topics to reject
    (parse never raises; validation reports).
    """
    if not any(_TOPIC_HEADING_RE.match(line) for line in text.splitlines()):
        return [
            {"id": f"T-{n['id']}", "title": n["title"], "objective": "",
             "tasks": [n]}
            for n in parse_graph_spec(text)
        ]
    topics: list[dict] = []
    current_topic: dict | None = None
    current_task: dict | None = None
    body: list[str] = []
    obj: list[str] = []

    def _flush_task():
        nonlocal current_task
        if current_task is not None:
            current_task["prompt"] = "\n".join(body).strip()
            current_topic["tasks"].append(current_task)
            current_task = None

    def _flush_topic():
        nonlocal current_topic
        if current_topic is not None:
            _flush_task()
            current_topic["objective"] = "\n".join(obj).strip()
            topics.append(current_topic)
            current_topic = None

    for line in text.splitlines():
        tm = _TOPIC_HEADING_RE.match(line)
        if tm:
            _flush_topic()
            current_topic = {"id": tm.group(1), "title": tm.group(2), "tasks": []}
            obj, body = [], []
            continue
        if current_topic is None:
            continue  # preamble
        if _HEADING_RE.match(line):
            # flat `## x:` heading inside a topic spec — mixed form, reject later
            current_topic["_mixed"] = True
            continue
        km = _TASK_HEADING_RE.match(line)
        if km:
            _flush_task()
            current_task = {"id": km.group(1), "title": km.group(2),
                            "deps": [], "verify_cmd": None}
            body = []
            continue
        fm = _FIELD_RE.match(line)
        if fm and current_task is not None:
            field, value = fm.group(1), fm.group(2).strip()
            if field == "deps":
                current_task["deps"] = [d.strip() for d in value.split(",") if d.strip()]
            else:
                current_task["verify_cmd"] = value or None
            continue
        (body if current_task is not None else obj).append(line)
    _flush_topic()
    return topics


def validate_topics(topics: list[dict]) -> list[str]:
    """Validation across both tiers. Reuses validate_graph for the task tier,
    then: mixed form, empty topics, duplicate topic ids, and a cycle in the
    DERIVED topic deps."""
    errors: list[str] = []
    if any(t.get("_mixed") for t in topics):
        errors.append("spec mixes `## topic` and flat `## node` headings — pick one form")
    tids = [t["id"] for t in topics]
    seen: set[str] = set()
    for tid in tids:
        if tid in seen:
            errors.append(f"duplicate topic id: {tid!r}")
        seen.add(tid)
    for t in topics:
        if not t["tasks"]:
            errors.append(f"topic {t['id']!r} has no tasks — it can never complete")
    all_tasks = [n for t in topics for n in t["tasks"]]
    errors += validate_graph(all_tasks) if all_tasks else ["spec has no tasks"]
    owner = {n["id"]: t["id"] for t in topics for n in t["tasks"]}
    tedges = sorted({
        (owner[n["id"]], owner[d])
        for t in topics for n in t["tasks"] for d in n["deps"]
        if d in owner and owner[d] != owner[n["id"]]
    })
    if not errors:
        cyc = find_cycle(tids, tedges)
        if cyc:
            errors.append(f"topic dependency cycle involving: {', '.join(cyc)}")
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
