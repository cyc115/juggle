"""juggle_cockpit_gitlog_meta — DB-aware metadata fetch for the full-screen
git-log graph view (cockpit-gitlog-view, part 2 of the 2026-07-01 graph
railroad work). Bulk-fetches merged_sha/verified_at (nodes) and the latest
agent_runs row (dispatched_at/completed_at/role/model) per node id — ONE query
each, no N+1 — so the pure ``juggle_cockpit_gitlog_lines`` renderer never
touches the DB. Read-only.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GitLogMeta:
    merged_sha: "str | None" = None
    verified_at: "str | None" = None
    dispatched_at: "str | None" = None
    completed_at: "str | None" = None
    role: "str | None" = None
    model: "str | None" = None


def gitlog_meta_for(db, ids: list[str]) -> dict[str, GitLogMeta]:
    """One GitLogMeta per id in ``ids`` (missing ids simply absent from the dict)."""
    ids = list(dict.fromkeys(ids))  # de-dupe, preserve order
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    conn = db._connect()
    try:
        node_rows = conn.execute(
            f"SELECT id, merged_sha, verified_at FROM nodes WHERE id IN ({ph})", ids
        ).fetchall()
        run_rows = conn.execute(
            "SELECT COALESCE(task_id, topic_id) AS nid, role, model, "
            "dispatched_at, completed_at, id AS run_id FROM agent_runs "
            f"WHERE task_id IN ({ph}) OR topic_id IN ({ph}) ORDER BY id DESC",
            [*ids, *ids],
        ).fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    base: dict[str, dict] = {r["id"]: dict(r) for r in node_rows}
    latest_run: dict[str, dict] = {}
    for r in run_rows:
        nid = r["nid"]
        if nid not in latest_run:  # ORDER BY id DESC — first hit is newest
            latest_run[nid] = dict(r)

    out: dict[str, GitLogMeta] = {}
    for nid in ids:
        n = base.get(nid, {})
        run = latest_run.get(nid, {})
        out[nid] = GitLogMeta(
            merged_sha=n.get("merged_sha"),
            verified_at=n.get("verified_at"),
            dispatched_at=run.get("dispatched_at"),
            completed_at=run.get("completed_at"),
            role=run.get("role"),
            model=run.get("model"),
        )
    return out
