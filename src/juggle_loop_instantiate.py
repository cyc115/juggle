"""juggle_loop_instantiate — instantiate a normalized loop-template topic into a
graph under a run-seq prefix (loop-entity V1).

Extracted (mechanical, behaviour-neutral) from ``juggle_cmd_loop_create`` so BOTH
the Phase-4 transactional create AND the Phase-5 watchdog re-fire share ONE writer
for the "materialize a template topic + its member tasks + dep edges under a
``<L#>-r<seq>-`` prefix" step. One source of truth for how an iteration's nodes are
laid down keeps create and re-fire from drifting.

Pure over the caller's ``conn`` — never commits (the caller owns the transaction).
Node ids are ``<prefix><base-id>`` so each iteration's ids are run-seq namespaced
and never collide with the guarded-upsert refusal (juggle_graph_load).
"""
from __future__ import annotations

from dbops import db_graph, db_topics


def instantiate_topic(db, conn, *, project_id: str, prefix: str, topic: dict):
    """Materialize normalized ``topic`` under ``prefix`` into ``project_id`` on the
    caller-supplied ``conn`` (no commit). Returns ``(topic_id, node_ids)``.

    ``topic`` shape (validator/reconstruct output): ``{id, title, objective, role,
    delivery, tasks: [{id, title, prompt, role, delivery, verify_cmd, deps}]}``.
    Only ``role``/``delivery`` are persisted per node (no nodes.model column in V1).
    """
    topic_id = f"{prefix}{topic['id']}"
    db_topics.create_topic(
        db, topic_id=topic_id, project_id=project_id,
        title=topic["title"], objective=topic.get("objective", ""), conn=conn,
    )
    conn.execute(
        "UPDATE nodes SET delivery=?, role=? WHERE id=? AND kind='topic'",
        (topic["delivery"], topic["role"], topic_id),
    )

    node_ids = [topic_id]
    for task in topic["tasks"]:
        tid = f"{prefix}{task['id']}"
        db_graph.create_task(
            db, task_id=tid, project_id=project_id, title=task["title"],
            prompt=task["prompt"], verify_cmd=task.get("verify_cmd"), conn=conn,
        )
        db_graph.set_task_topic(db, tid, topic_id, conn=conn)
        conn.execute(
            "UPDATE nodes SET role=?, delivery=? WHERE id=? AND kind='task'",
            (task["role"], task["delivery"], tid),
        )
        node_ids.append(tid)

    for task in topic["tasks"]:
        deps = sorted(f"{prefix}{d}" for d in task.get("deps", []))
        if deps:
            db_graph.replace_edges(db, f"{prefix}{task['id']}", deps, conn=conn)

    return topic_id, node_ids
