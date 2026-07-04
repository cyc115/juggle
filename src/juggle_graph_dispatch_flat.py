"""juggle_graph_dispatch_flat — legacy flat-task fallback dispatch (R9/R6).

Extracted from juggle_graph_dispatch (2026-07-04, loop-entity Phase 2 LOC gate:
the dispatcher module was at its 410-line allowlist budget and Phase 2's
per-node role resolution needed headroom). ZERO behaviour change — the
``_dispatch_flat_task_fallback`` body is moved verbatim; juggle_graph_dispatch
re-exports it (bottom import) so ``gd._dispatch_flat_task_fallback`` and the
tick keep calling it unchanged.

The claim/sweep/give-up helpers this module needs live in juggle_graph_dispatch;
they are imported LAZILY inside the function (not at module top) so this module
is import-safe in either order — a top-level ``from juggle_graph_dispatch import``
would deadlock the cycle when flat is imported before gd.

Owns: dispatching ready graph tasks that have NO graph-topic home (parentless
orphans) — the belt-and-suspenders so a parentless task never silently stalls
a project.
Must not own: the topic tick loop / hydration / claim semantics
(juggle_graph_dispatch), task state semantics (dbops.db_graph).
"""

from __future__ import annotations

import logging

from dbops import db_graph
from dbops import event_kinds as _ek

_log = logging.getLogger("juggle-graph-dispatch")


def _dispatch_flat_task_fallback(
    db, all_projects: list[str], stats: dict, dispatch
) -> None:
    """Dispatch ready graph tasks that have NO graph-topic home (parent_id NULL).

    Belt-and-suspenders so a parentless orphan never silently stalls a project
    (2026-06-30 orphan-task dispatch gap). Covers both a flat project with ZERO
    graph-topics (every task parentless) and a stray orphan in a project that
    HAS (completed) topics — the old code skipped the whole project the moment it
    had ≥1 topic, stranding these tasks forever. A task WITH a topic parent is the
    topic path's job (the topic agent carries all its members), never re-dispatched
    here — so ONLY parentless (topic_id NULL) tasks are true orphans."""
    import juggle_graph_dispatch as _gd  # lazy — breaks the flat⇄gd import cycle
    from juggle_graph_hydration import hydrate_for_task

    for pid in all_projects:
        try:
            stats["swept"] += _gd.sweep_stale_claims(db, pid)
            db_graph.recompute_ready(db, pid)
            tasks = [
                n
                for n in db_graph.list_tasks(db, pid)
                if n["state"] == "ready" and n.get("topic_id") is None
            ]
        except Exception:
            _log.exception(
                "graph tick (flat fallback): ready-set scan failed for %s", pid
            )
            continue
        for task in tasks:
            task_id = task["id"]
            fail_key = (str(db.db_path), task_id)
            try:
                if not _gd.claim_task(db, task_id):
                    continue
                try:
                    thread_id = db.create_thread(
                        f"[{task_id}] {task['title']}"[:80],
                        session_id=_gd._session_id(db),
                        project_id=pid,
                    )
                except ValueError as e:
                    db_graph.task_transition(db, task_id, "stale_reset")
                    if "Maximum of" not in str(e):
                        stats["errors"].append(task_id)
                    else:
                        stats["deferred"].append(task_id)
                        _log.info("graph tick (flat): thread cap — task %s deferred", task_id)
                    continue
                db.update_thread(thread_id, project_id=pid)
                db_graph.set_task_thread(db, task_id, thread_id)
                try:
                    dispatch(db, thread_id, hydrate_for_task(db, pid, task), task)
                except _gd.CapacityError:
                    db.archive_thread(thread_id)
                    db_graph.set_task_thread(db, task_id, None)
                    db_graph.task_transition(db, task_id, "stale_reset")
                    stats["deferred"].append(task_id)
                    break
                except Exception as e:
                    db.archive_thread(thread_id)
                    db_graph.set_task_thread(db, task_id, None)
                    stats["errors"].append(task_id)
                    fails = _gd._dispatch_fails.get(fail_key, 0) + 1
                    _gd._dispatch_fails[fail_key] = fails
                    if fails >= _gd.MAX_DISPATCH_FAILS:
                        _gd._dispatch_fails.pop(fail_key, None)
                        _gd._give_up_dispatch(db, task_id, e)
                    else:
                        db_graph.task_transition(db, task_id, "stale_reset")
                    continue
                _gd._dispatch_fails.pop(fail_key, None)
                db_graph.task_transition(db, task_id, "dispatch")
                db.emit_event(
                    thread_id=thread_id, session_id=_gd._session_id(db), kind=_ek.AUTOPILOT_DISPATCH,
                    message=f"⬢ autopilot dispatched task {task_id} — {task['title']}",
                )
                stats["dispatched"].append(task_id)
            except Exception:
                _log.exception("graph tick (flat fallback): error on task %s", task_id)
                stats["errors"].append(task_id)
