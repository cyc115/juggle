"""juggle_graph_scheduler — fair cross-project dispatch ordering (pure).

Owns: the least-loaded-first round-robin interleave deciding in which ORDER
ready TOPICS are claimed when several projects are armed (R3/R9 — the topic is
the budget unit: one topic = one thread = one agent; tasks are sequential
inside their topic and never scheduled here). The global capacity cap stays in
the dispatch path; because this ordering is fair, the prefix that fits under
the cap is fair too (a cap hit breaks the whole pass, spec DA A5).
Must not own: DB access, claiming, dispatching (juggle_graph_dispatch).

Policy (spec §2.7): sort armed projects by in-flight topic count ascending
(tie-break: arm order), then emit ready topics one per project per round.
Stateless + deterministic — no persisted cursor; self-balancing because last
tick's winners carry higher in-flight counts.
"""

from __future__ import annotations


def interleave_ready(
    ready_by_project: dict[str, list[dict]],
    in_flight: dict[str, int],
    armed_order: list[str],
) -> list[tuple[str, dict]]:
    """Fair cross-project dispatch order: list of (project_id, topic).

    Within each project the ready queue is stably pre-sorted by ``priority``
    DESC (T-fix-priority-dispatch-ordering) so a fix/defect topic outranks a
    feature topic filed earlier; equal-priority topics keep their incoming
    (created_at, id) order. This is the SINGLE dispatch-ordering source — the
    cross-project fair interleave below is unchanged.
    """
    rank = {pid: i for i, pid in enumerate(armed_order)}
    pids = [p for p in ready_by_project if ready_by_project[p]]
    pids.sort(key=lambda p: (in_flight.get(p, 0), rank.get(p, len(rank))))
    queues = {
        p: sorted(ready_by_project[p], key=lambda t: -t.get("priority", 0))
        for p in pids
    }
    out: list[tuple[str, dict]] = []
    while queues:
        for pid in [p for p in pids if p in queues]:
            out.append((pid, queues[pid].pop(0)))
            if not queues[pid]:
                del queues[pid]
    return out
