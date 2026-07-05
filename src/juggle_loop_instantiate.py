"""juggle_loop_instantiate — materialize a loop's STABLE topic + its per-run task
generations into the graph (loop-entity V2 stable-topic model, §0b).

Two seams, both pure over the caller's ``conn`` (never commit — the caller owns the
transaction):

  * ``create_stable_topic`` — create the loop's ONE long-lived ``kind='topic'`` node
    (a STABLE id, ``<L#>-<topic>``, no run prefix). Called ONCE at loop-create time.
  * ``instantiate_generation`` — materialize a run-namespaced TASK generation
    (``<L#>-r<seq>-<task>``) parented under an EXISTING stable topic. Called at
    create (r0) AND every fire (r<seq>). Task ids are run-seq namespaced so a fresh
    generation never collides with a prior generation's PROTECTED terminal nodes
    (the guarded-upsert refusal, juggle_graph_load) — the sole reason run_seq exists.

V2 delta from V1: V1 minted a fresh run-namespaced TOPIC per fire; V2 keeps the
topic identity STABLE and makes only the task generations ephemeral (§0b).
Only ``role``/``delivery`` are persisted per node (no nodes.model column yet).
"""
from __future__ import annotations

from dbops import db_graph, db_topics


def create_stable_topic(db, conn, *, project_id: str, topic_id: str, topic: dict) -> str:
    """Create the loop's ONE stable ``kind='topic'`` node under ``topic_id`` on the
    caller-supplied ``conn`` (no commit). Sets role/delivery. Returns ``topic_id``.

    ``topic`` shape (validator/reconstruct output): ``{id, title, objective, role,
    delivery, tasks: [...]}``.
    """
    db_topics.create_topic(
        db, topic_id=topic_id, project_id=project_id,
        title=topic["title"], objective=topic.get("objective", ""), conn=conn,
    )
    conn.execute(
        "UPDATE nodes SET delivery=?, role=? WHERE id=? AND kind='topic'",
        (topic["delivery"], topic["role"], topic_id),
    )
    return topic_id


def instantiate_generation(db, conn, *, project_id: str, topic_id: str,
                           gen_prefix: str, tasks: list) -> list:
    """Materialize a run-namespaced TASK generation (``<gen_prefix><task>``) parented
    under the EXISTING stable ``topic_id`` on the caller's ``conn`` (no commit). Does
    NOT touch the topic node — the caller owns its lifecycle (create / reopen).
    Returns the list of created task node ids.
    """
    node_ids = []
    for task in tasks:
        tid = f"{gen_prefix}{task['id']}"
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

    for task in tasks:
        deps = sorted(f"{gen_prefix}{d}" for d in task.get("deps", []))
        if deps:
            db_graph.replace_edges(db, f"{gen_prefix}{task['id']}", deps, conn=conn)

    return node_ids
