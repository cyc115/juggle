"""juggle_graph_status — read-only display aggregates over graph_tasks.

Owns: per-project task-state counts, the cockpit progress string
("3/14 done, 1 failed, 2 ready" — DA m2), and the char-budgeted
UserPromptSubmit graph-status injection (HARD 500 chars, DA m4).
Must not own: task state semantics (dbops.db_graph), dispatching
(juggle_graph_dispatch), or any state writes — this module is read-only.
"""

from __future__ import annotations

FAILED_STATES = ("failed-exec", "failed-integration", "failed-verify")
# dispatching/integrating are transient execution states — fold into "running"
# for display so operators see "in flight", not scheduler internals.
IN_FLIGHT_STATES = ("dispatching", "running", "integrating")

INJECTION_BUDGET = 500  # HARD cap on injected graph status (DA m4)
_ELLIPSIS = "…"


def counts_from_states(states: list[str]) -> dict:
    """Aggregate raw graph_tasks.state values into display counts. Pure."""
    return {
        "total": len(states),
        "verified": sum(1 for s in states if s == "verified"),
        "failed": sum(1 for s in states if s in FAILED_STATES),
        "blocked": sum(1 for s in states if s == "blocked-failed"),
        "ready": sum(1 for s in states if s == "ready"),
        "running": sum(1 for s in states if s in IN_FLIGHT_STATES),
        "pending": sum(1 for s in states if s == "pending"),
    }


def graph_counts(db, project_id: str) -> dict | None:
    """Counts for ``project_id``, or None (no tasks / pre-migration DB)."""
    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT state FROM graph_tasks WHERE project_id=?", (project_id,)
            ).fetchall()
    except Exception:
        return None  # pre-migration DB without graph_tasks
    states = [r[0] for r in rows]
    return counts_from_states(states) if states else None


def format_progress(counts: dict) -> str:
    """'3/14 done, 1 failed, 2 ready' — zero segments after done are omitted."""
    parts = [f"{counts['verified']}/{counts['total']} done"]
    for key in ("failed", "blocked", "ready", "running"):
        if counts.get(key):
            parts.append(f"{counts[key]} {key}")
    return ", ".join(parts)


def _titled(db, project_id: str, states: tuple[str, ...]) -> list[tuple[str, str]]:
    with db._connect() as conn:
        ph = ",".join("?" * len(states))
        rows = conn.execute(
            f"SELECT id, title FROM graph_tasks WHERE project_id=? "
            f"AND state IN ({ph}) ORDER BY created_at, id",
            (project_id, *states),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def build_graph_injection(db, project_id: str, budget: int = INJECTION_BUDGET) -> str:
    """Graph status line for the armed project, HARD-capped at ``budget`` chars.

    Counts + ready/running task titles; truncation is deterministic (fixed
    ordering + hard slice with ellipsis), so the same DB state always injects
    the same text (DA m4).
    """
    counts = graph_counts(db, project_id)
    if counts is None:
        text = f"Graph [{project_id}]: no graph loaded yet (juggle project-graph load)."
    else:
        segs = [f"Graph [{project_id}]: {format_progress(counts)}."]
        ready = _titled(db, project_id, ("ready",))
        if ready:
            segs.append("ready: " + "; ".join(f"{i} ({t})" for i, t in ready) + ".")
        running = _titled(db, project_id, IN_FLIGHT_STATES)
        if running:
            segs.append("running: " + "; ".join(f"{i} ({t})" for i, t in running) + ".")
        text = " ".join(segs)
    if len(text) > budget:
        text = text[: budget - len(_ELLIPSIS)] + _ELLIPSIS
    return text
