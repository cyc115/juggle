"""juggle_cockpit_graph_activity — the DB-reading side of the cockpit graph-panel
project ordering (extracted from juggle_cockpit_graph_dag for the LOC gate, P3b
extract-first, 2026-07-05).

Owns ``gather_project_activity``: derive one ``ProjectActivity`` ordering row per
candidate project (active ∪ any project referenced by a root graph node) purely
from the DB. The pure ordering POLICY lives in ``juggle_cockpit_graph_order``
(``order_projects``); the DAG-STRUCTURE loader (``_load_one`` / ``load_graph_dags``)
stays in ``juggle_cockpit_graph_dag`` and imports this back. Read-only; every read
is fail-soft (degrades to ``[]`` on a pre-migration / broken DB, never raises).
"""
from __future__ import annotations

from juggle_cockpit_graph_order import ProjectActivity


def _norm_ts(ts: "str | None") -> str:
    """Fold a stored timestamp to 'YYYY-MM-DD HH:MM' so keys compare
    chronologically. The DB mixes isoformat ('T' separator, projects.last_active)
    with strftime (space, conversation/verify); 'T'(0x54) > ' '(0x20) would
    misorder same-minute rows. All UTC, so truncating to the minute is safe."""
    return (ts or "").replace("T", " ")[:16]


def gather_project_activity(conn) -> list[ProjectActivity]:
    """Ordering rows for every candidate project, derived purely from the DB.

    Candidates = active projects ∪ any project referenced by a root graph node.
    ``is_done`` = has root nodes AND none non-verified. ``active_key`` = max
    conversation ``last_active_at`` over the project's dispatch-bound topics,
    floored by ``last_active``. ``done_key`` = max ``verified_at``, same floor
    (ISO strings compare chronologically). Every read is fail-soft.
    """
    try:
        proj_rows = conn.execute(
            "SELECT id, last_active FROM projects WHERE status='active' AND kind != 'loop'"
        ).fetchall()
    except Exception:
        return []
    last_active: dict[str, str] = {r[0]: (r[1] or "") for r in proj_rows}
    candidates: list[str] = list(last_active.keys())

    try:
        for r in conn.execute(
            "SELECT DISTINCT project_id FROM nodes "
            "WHERE kind IN ('topic','task','research') AND parent_id IS NULL "
            "AND project_id IS NOT NULL AND project_id NOT IN (SELECT id FROM projects WHERE kind='loop')"
        ).fetchall():
            pid = r[0]
            if pid and pid not in last_active:
                last_active[pid] = ""
                candidates.append(pid)
    except Exception:
        pass

    # Root-node aggregates: open (non-verified) count, total, max verified_at.
    open_count: dict[str, int] = {}
    root_total: dict[str, int] = {}
    verified_max: dict[str, str] = {}
    try:
        for r in conn.execute(
            "SELECT project_id, "
            "SUM(CASE WHEN state NOT IN ('verified','delivered','cancelled') THEN 1 ELSE 0 END) AS opn, "
            "COUNT(*) AS total, MAX(COALESCE(verified_at,'')) AS vmax "
            "FROM nodes "
            "WHERE (kind='topic' OR (kind='task' AND parent_id IS NULL)) "
            "AND project_id IS NOT NULL GROUP BY project_id"
        ).fetchall():
            open_count[r[0]] = r[1] or 0
            root_total[r[0]] = r[2] or 0
            verified_max[r[0]] = r[3] or ""
    except Exception:
        pass

    # Live agent activity: max conversation last_active_at over dispatch-bound topics.
    agent_ts: dict[str, str] = {}
    try:
        for r in conn.execute(
            "SELECT t.project_id AS pid, MAX(COALESCE(c.last_active_at,'')) AS ts "
            "FROM nodes t "
            "JOIN node_edges de ON de.node_id = t.id AND de.kind='dispatch' "
            "JOIN nodes c ON c.id = de.depends_on_id AND c.kind='conversation' "
            "WHERE t.kind='topic' AND t.project_id IS NOT NULL GROUP BY t.project_id"
        ).fetchall():
            if r[0]:
                agent_ts[r[0]] = r[1] or ""
    except Exception:
        pass

    rows: list[ProjectActivity] = []
    for pid in candidates:
        la = _norm_ts(last_active.get(pid, ""))
        agent = _norm_ts(agent_ts.get(pid, ""))
        verified = _norm_ts(verified_max.get(pid, ""))
        is_done = root_total.get(pid, 0) > 0 and open_count.get(pid, 0) == 0
        rows.append(
            ProjectActivity(
                id=pid,
                is_done=is_done,
                # Agent-activity if any conversation ran, else last_active (spec
                # FALLBACK not max — a recent creation ts never outranks active work).
                active_key=agent or la,
                done_key=max(verified, la),
            )
        )
    return rows
