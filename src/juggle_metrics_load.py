"""juggle_metrics_load — load + slice agent_runs rows for the metrics rollup
(2026-06-30 orchestration-metrics). Read-only; deterministic."""
from __future__ import annotations

_BY_COL = {"prompt-version": "prompt_version", "role": "role",
           "project": "project_id", "model": "model"}


def load_runs(db, *, since: str | None = None) -> list[dict]:
    sql = "SELECT * FROM agent_runs"
    params: list = []
    if since:
        sql += " WHERE dispatched_at >= ?"
        params.append(since)
    sql += " ORDER BY id DESC"
    with db._connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def group_runs(runs: list[dict], by: str | None) -> dict:
    if by is None:
        return {"all": list(runs)}
    col = _BY_COL.get(by)
    if col is None:
        raise ValueError(f"unknown --by key: {by!r} (choose {sorted(_BY_COL)})")
    out: dict = {}
    for r in runs:
        out.setdefault(r.get(col) or "∅", []).append(r)
    return out
