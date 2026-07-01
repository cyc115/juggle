"""juggle_metrics_errors — auto-derived orchestrator error rate over agent_runs
+ nodes + error_events (2026-06-30 orchestration-metrics §4). Each component is a
deterministic count with a named source, returned as a dict so every one is
independently verifiable. Phase-1 = failed + rework + blocked + selfheal_classb
(a documented, consistent UNDER-count; stale-reset/wrong-target deferred)."""
from __future__ import annotations


def _failed(runs: list[dict]) -> int:
    return sum(1 for r in runs if (r.get("status") or "") == "failed")


def _rework(runs: list[dict]) -> int:
    """Σ (dispatch_count − 1) over task_ids RE-DISPATCHED (>1 run). Distinct
    task_ids each dispatched once (planned multi-node work) contribute 0."""
    counts: dict = {}
    for r in runs:
        tid = r.get("task_id")
        if tid:
            counts[tid] = counts.get(tid, 0) + 1
    return sum(c - 1 for c in counts.values() if c > 1)


def _blocked(db, runs: list[dict]) -> int:
    """Distinct task_id among the runs whose node is in 'blocked-failed'."""
    ids = {r.get("task_id") for r in runs if r.get("task_id")}
    if not ids:
        return 0
    try:
        with db._connect() as conn:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT DISTINCT id FROM nodes WHERE state='blocked-failed' "
                f"AND id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
        return len(rows)
    except Exception:
        return 0


def _window(runs: list[dict]) -> tuple[str | None, str | None]:
    ds = [r.get("dispatched_at") for r in runs if r.get("dispatched_at")]
    cs = [r.get("completed_at") for r in runs if r.get("completed_at")]
    return (min(ds) if ds else None, max(cs) if cs else None)


def _selfheal_classb(db, runs: list[dict]) -> int:
    """error_events rows error_class='B' (Juggle-caused) with last_seen in the
    runs' [min dispatched, max completed] window. 0 when the table is absent."""
    lo, hi = _window(runs)
    if not lo or not hi:
        return 0
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM error_events WHERE error_class='B' "
                "AND last_seen >= ? AND last_seen <= ?",
                (lo, hi),
            ).fetchone()
        return int(row["n"]) if row else 0
    except Exception:
        return 0


def error_breakdown(db, runs: list[dict]) -> dict:
    """§4 components + total/dispatches/rate. rate = total ÷ dispatches (0.0 guard)."""
    failed = _failed(runs)
    rework = _rework(runs)
    blocked = _blocked(db, runs)
    classb = _selfheal_classb(db, runs)
    total = failed + rework + blocked + classb
    dispatches = len(runs)
    rate = round(total / dispatches, 6) if dispatches else 0.0
    return {"failed": failed, "rework": rework, "blocked": blocked,
            "selfheal_classb": classb, "total": total,
            "dispatches": dispatches, "rate": rate}
