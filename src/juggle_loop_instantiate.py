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


def _roots(tasks: list) -> list:
    """Base ids of a topic's ENTRY tasks (no intra-topic deps). Falls back to ALL
    tasks if none (a task-level cycle would leave no root — degrade to a safe
    over-connection rather than emitting zero crossing edges)."""
    roots = [t["id"] for t in tasks if not t.get("deps")]
    return roots or [t["id"] for t in tasks]


def _leaves(tasks: list) -> list:
    """Base ids of a topic's TERMINAL tasks (no sibling depends on them). Falls back
    to ALL tasks if none, for the same reason as ``_roots``."""
    depended = {d for t in tasks for d in t.get("deps", [])}
    leaves = [t["id"] for t in tasks if t["id"] not in depended]
    return leaves or [t["id"] for t in tasks]


def wire_cross_topic_edges(db, conn, *, gen_prefix: str, topics: list) -> None:
    """Translate the template's TOPIC-level deps into crossing TASK edges (spec §6/§1),
    on the caller's ``conn`` (no commit).

    A topic-level dep ``B deps-on A`` is realized as edges from B's ROOT gen tasks to
    A's LEAF gen tasks (``<gen_prefix><root> -> <gen_prefix><leaf>``). That single
    crossing edge is what ``db_topics.derived_topic_deps`` reads to surface the
    topic-level dependency AND what ``topic_ready_eligible`` gates on (topic B stays
    'open' until topic A is verified/delivered), so a multi-topic loop's downstream
    topic hydrates the upstream topic's handoff pointer (§1) only after it completes.

    Task ids are GLOBALLY unique across topics (validator guarantee), so
    ``<gen_prefix><task_id>`` is unambiguous. Existing intra-topic edges are preserved
    (read on THIS conn so uncommitted writes are visible, then UNION-replaced)."""
    by_id = {t["id"]: t for t in topics}
    for topic in topics:
        roots = [f"{gen_prefix}{rid}" for rid in _roots(topic["tasks"])]
        for up in topic.get("deps", []):
            up_leaves = [f"{gen_prefix}{lid}" for lid in _leaves(by_id[up]["tasks"])]
            for node_id in roots:
                existing = [
                    r[0] for r in conn.execute(
                        "SELECT depends_on_id FROM node_edges "
                        "WHERE node_id=? AND kind='dep'",
                        (node_id,),
                    ).fetchall()
                ]
                db_graph.replace_edges(
                    db, node_id, sorted(set(existing) | set(up_leaves)), conn=conn,
                )
