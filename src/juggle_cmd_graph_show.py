"""juggle_cmd_graph_show — `juggle graph show` READ command (graph-node-primitives
Phase4, 2026-07-03 spec §1).

Pure read over the task graph — zero mutation, no migration. Three views:
  * no args        → one dense line per project (done/total ✓ · failed⊘ …)
  * --project P    → project header + one line per node (deps + dependents count)
  * --node ID      → single-node detail (state, deps, dependents, verify_cmd,
                     verify_failure ≤200 chars, cancel_reason in --json)
`--state S` filters the node list; `--json` emits compact machine-readable output.

Must not own: any state write (that is db_graph.task_transition's job) or the
mutator handlers (juggle_cmd_graph_ops). get_db is resolved through
juggle_cmd_graph at call time so test monkeypatches (cg.get_db) keep working.
"""
from __future__ import annotations

import json
import sys

from dbops import db_graph

# State glyphs for `show` (spec §22). Every real lifecycle state (db_graph
# VALID_STATES + the future 'cancelled') maps to one of the six display glyphs;
# unknown states fall back to '·'.
FAILED_STATES = frozenset({"failed-exec", "failed-integration", "failed-verify"})
SHOW_STATE_GLYPHS: dict[str, str] = {
    "open": "○", "ready": "○",
    "dispatching": "●", "running": "●", "integrating": "●",
    "integrated-unlanded": "●",
    "verified": "✓",
    "failed-exec": "✗", "failed-integration": "✗", "failed-verify": "✗",
    "blocked-failed": "⊘",
    "cancelled": "⊗",
}


def _glyph(state: str) -> str:
    return SHOW_STATE_GLYPHS.get(state, "·")


def _truncate(text, limit: int = 200) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _counts(tasks: list[dict]) -> dict:
    """Rollup counts. 'cancelled' is excluded from done/total (spec B1)."""
    total = sum(1 for t in tasks if t["state"] != "cancelled")
    done = sum(1 for t in tasks if t["state"] == "verified")
    failed = sum(1 for t in tasks if t["state"] in FAILED_STATES)
    blocked = sum(1 for t in tasks if t["state"] == "blocked-failed")
    cancelled = sum(1 for t in tasks if t["state"] == "cancelled")
    return {"done": done, "total": total, "failed": failed,
            "blocked": blocked, "cancelled": cancelled}


def _project_line(pid: str, name: str, c: dict) -> str:
    parts = [f"{c['done']}/{c['total']} ✓"]
    extra = []
    if c["failed"]:
        extra.append(f"{c['failed']}✗")
    if c["blocked"]:
        extra.append(f"{c['blocked']}⊘")
    if c["cancelled"]:
        extra.append(f"{c['cancelled']}⊗")
    if extra:
        parts.append("· " + " ".join(extra))
    return f"{pid}  {name}  {' '.join(parts)}"


def _node_line(db, task: dict) -> str:
    deps = db_graph.get_deps(db, task["id"])
    dependents = db_graph.get_dependents(db, task["id"])
    dep_str = ",".join(deps) if deps else "—"
    return (f"  {_glyph(task['state'])} {task['id']}  {task['title']}  "
            f"(deps: {dep_str} · dependents: {len(dependents)})")


def _print_node_detail(db, task: dict) -> None:
    deps = db_graph.get_deps(db, task["id"])
    dependents = db_graph.get_dependents(db, task["id"])
    print(f"{_glyph(task['state'])} {task['id']}  {task['title']}")
    print(f"  state:        {task['state']}")
    print(f"  deps:         {', '.join(deps) if deps else '—'}")
    print(f"  dependents:   {', '.join(dependents) if dependents else '—'}")
    print(f"  verify_cmd:   {task.get('verify_cmd') or '—'}")
    print(f"  verify_failure: {_truncate(task.get('verify_failure')) or '—'}")
    print(f"  updated_at:   {task.get('updated_at') or '—'}")


def _node_json(db, task: dict) -> dict:
    return {
        "id": task["id"],
        "title": task["title"],
        "state": task["state"],
        "deps": db_graph.get_deps(db, task["id"]),
        "dependents": db_graph.get_dependents(db, task["id"]),
        "verify_cmd": task.get("verify_cmd"),
        "verify_failure": task.get("verify_failure"),
        # cancel_reason column lands with the spine migration; .get() keeps this
        # forward-compatible (None until then) without a schema dependency.
        "cancel_reason": task.get("cancel_reason"),
        "updated_at": task.get("updated_at"),
    }


def cmd_graph_show(args) -> None:
    """`juggle graph show [--project P] [--node ID] [--state S] [--json]` — read."""
    import juggle_cmd_graph as cg  # lazy: reuse the shared get_db patch surface

    db = cg.get_db(getattr(args, "db_path", None), init=False)
    json_out = getattr(args, "json_out", False)
    node = getattr(args, "node", None)
    project = getattr(args, "project", None)
    state = getattr(args, "state", None)

    # ── single-node detail ──
    if node:
        task = db_graph.get_task(db, node)
        if not task:
            print(f"Error: node {node!r} not found.", file=sys.stderr)
            sys.exit(1)
        if json_out:
            print(json.dumps(_node_json(db, task)))
        else:
            _print_node_detail(db, task)
        return

    # ── project view ──
    if project:
        if not db.get_project(project):
            print(f"Error: project {project!r} not found.", file=sys.stderr)
            sys.exit(1)
        tasks = db_graph.list_tasks(db, project)
        shown = [t for t in tasks if not state or t["state"] == state]
        if json_out:
            c = _counts(tasks)
            print(json.dumps({
                "project": project, "done": c["done"], "total": c["total"],
                "nodes": [{
                    "id": t["id"], "title": t["title"], "state": t["state"],
                    "deps": db_graph.get_deps(db, t["id"]),
                    "dependents": db_graph.get_dependents(db, t["id"]),
                } for t in shown],
            }))
            return
        name = db.get_project(project).get("name") or project
        print(_project_line(project, name, _counts(tasks)))
        for t in shown:
            print(_node_line(db, t))
        return

    # ── all-projects rollup (or flat --state filter across projects) ──
    projects = db.list_projects(include_archived=True)
    if state:
        # flat: every node in `state` across all projects (human line or JSON)
        matched = [t for p in projects
                   for t in db_graph.list_tasks(db, p["id"]) if t["state"] == state]
        if json_out:
            print(json.dumps({"state": state, "nodes": [{
                "id": t["id"], "title": t["title"], "state": t["state"],
                "project": t["project_id"],
                "deps": db_graph.get_deps(db, t["id"]),
                "dependents": db_graph.get_dependents(db, t["id"]),
            } for t in matched]}))
        else:
            for t in matched:
                print(_node_line(db, t))
        return
    rows = []
    for p in projects:
        tasks = db_graph.list_tasks(db, p["id"])
        if not tasks:
            continue
        c = _counts(tasks)
        rows.append((p["id"], p.get("name") or p["id"], c))
    if json_out:
        print(json.dumps({"projects": [
            {"project": pid, "name": name, "done": c["done"], "total": c["total"]}
            for pid, name, c in rows
        ]}))
        return
    for pid, name, c in rows:
        print(_project_line(pid, name, c))
