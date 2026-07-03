"""node_detail_text / topic_detail_text — structured detail-pane text for the
Frontier Railroad's bottom pane. node_detail_text was extracted from the
removed RailroadScreen (Surface-B, 2026-06-30); topic_detail_text is new
(fr-vf-polish, 2026-07-03) — the topic tier previously called node_detail_text
with a topic id, which db_graph.get_task only resolves for kind='task' rows,
so every field rendered blank (the "Task irl-backbone / title <blank>"
screenshot bug). Both return Rich markup for a markup=True Static; freeform DB
text is escaped via juggle_frontier_rails.style (single escaping seam)."""
from __future__ import annotations

from juggle_frontier_layout import FAILED_STATES, IN_FLIGHT_STATES
from juggle_frontier_rails import DIM, style

_RULE = style("─" * 40, DIM)


def _state_color(state: str) -> "str | None":
    from juggle_frontier_rails import AMBER, GREEN, RED

    if state in IN_FLIGHT_STATES:
        return AMBER
    if state in FAILED_STATES:
        return RED
    if state in ("ready", "verified"):
        return GREEN
    return None


def _field(label: str, value: str) -> str:
    return f"{style(label.ljust(8), DIM)} {style(value)}"


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
    state = task.get("state", "")
    lines = [
        style(f"Task {task.get('id', task_id)}", None, bold=True),
        _RULE,
        _field("title", task.get("title", "") or "(none)"),
        f"{style('state'.ljust(8), DIM)} {style(state or '(none)', _state_color(state))}",
        _field("deps", ", ".join(deps) if deps else "(none)"),
        _field("thread", task.get("thread_id") or "(unbound)"),
        _field("verify", task.get("verify_cmd") or "(none)"),
    ]
    try:
        runs = db.get_runs(task_id=task_id)
    except Exception:
        runs = []
    if runs:
        toks = sum((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in runs)
        lines += ["", _field("runs", f"{len(runs)} ({toks} tok)")]
    prompt = (task.get("prompt") or "").strip()
    if prompt:
        lines += ["", style("prompt:", DIM), style(prompt[:400])]
    handoff = (task.get("handoff") or "").strip()
    if handoff:
        lines += ["", style("handoff:", DIM), style(handoff[:400])]
    return "\n".join(lines)


def topic_detail_text(db, topic_id: str) -> str:
    """Structured detail text for a TOPIC node — title / state / member-task
    n/m with a per-state breakdown, the member task list (id · colored state
    glyph), and a runs/token rollup across the topic AND every member task.
    Never blank: missing fields render "(none)"."""
    from juggle_cockpit_legend import railroad_glyph

    with db._connect() as conn:
        # No kind filter: a project-root DAG vertex can be a bare kind='task'
        # node with no owning topic (P8 M2) -- it still needs a non-blank
        # detail pane, it just has no member tasks (its own children query
        # below naturally returns none).
        row = conn.execute(
            "SELECT id, title, state FROM nodes WHERE id=?", (topic_id,),
        ).fetchone()
        children = conn.execute(
            "SELECT id, title, state FROM nodes WHERE parent_id=? ORDER BY created_at, id",
            (topic_id,),
        ).fetchall()

    title = (row["title"] if row else None) or "(none)"
    state = (row["state"] if row else None) or "(none)"
    total = len(children)
    done = sum(1 for c in children if c["state"] == "verified")
    counts: dict[str, int] = {}
    for c in children:
        counts[c["state"]] = counts.get(c["state"], 0) + 1
    breakdown = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())) if counts else "(none)"

    lines = [
        style(f"Topic {topic_id}", None, bold=True),
        _RULE,
        _field("title", title),
        f"{style('state'.ljust(8), DIM)} {style(state, _state_color(state))}",
        _field("tasks", f"{done}/{total} ({breakdown})" if total else "(none)"),
    ]

    if children:
        lines += ["", style("member tasks:", DIM)]
        for c in children:
            glyph = railroad_glyph(c["state"])
            lines.append(f"  {style(glyph, _state_color(c['state']))} {style(c['id'])} {style(c['title'] or '', DIM)}")

    runs: list[dict] = []
    try:
        runs += db.get_runs(topic_id=topic_id)
    except Exception:
        pass
    for c in children:
        try:
            runs += db.get_runs(task_id=c["id"])
        except Exception:
            pass
    if runs:
        toks = sum((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in runs)
        lines += ["", _field("runs", f"{len(runs)} ({toks} tok)")]

    return "\n".join(lines)
