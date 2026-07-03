"""node_detail_text — structured task-node detail text, extracted from the
removed RailroadScreen (Surface-B, 2026-06-30) so the Frontier Railroad
(fr-screen) can reuse it for its bottom node-detail pane."""
from __future__ import annotations


def node_detail_text(db, task_id: str) -> str:
    """Structured detail text for a task node — id / title / state / deps /
    thread / verify, a run+token rollup, and prompt/handoff excerpts. Mirrors
    ``_NodeDetailModal._field_lines`` (task branch); PURE string assembly."""
    from dbops import db_graph
    from dbops.db_graph_edges import get_deps

    task = db_graph.get_task(db, task_id) or {}
    try:
        deps = get_deps(db, task_id)
    except Exception:
        deps = []
    lines = [
        f"Task {task.get('id', task_id)}",
        "─" * 40,
        f"title    {task.get('title', '')}",
        f"state    {task.get('state', '')}",
        f"deps     {', '.join(deps) if deps else '(none)'}",
        f"thread   {task.get('thread_id') or '(unbound)'}",
        f"verify   {task.get('verify_cmd') or '(none)'}",
    ]
    try:
        runs = db.get_runs(task_id=task_id)
    except Exception:
        runs = []
    if runs:
        toks = sum((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in runs)
        lines += ["", f"runs     {len(runs)} ({toks} tok)"]
    prompt = (task.get("prompt") or "").strip()
    if prompt:
        lines += ["", "prompt:", prompt[:400]]
    handoff = (task.get("handoff") or "").strip()
    if handoff:
        lines += ["", "handoff:", handoff[:400]]
    return "\n".join(lines)
